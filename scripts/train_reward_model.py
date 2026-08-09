#!/usr/bin/env python3
"""
SkillOpt reward model training script - v1.2.0 SKELETON.

ROADMAP Day-3, item 3 (reward model).

This script trains a 3-class DistilBERT classifier (success / partial /
failure) on labelled agent rollouts. The model is then used by
`helpers/reward_model.py` to score every new rollout produced by the
harvester.

The script is intentionally a SKELETON for v1.2.0. The real training
loop needs:
  1. ~5K labelled rollouts. The labels come from the A/B harness's
     LLM judge (Day-3 item 2, not yet implemented). Until that
     harness exists, we cannot produce trustworthy labels at scale.
  2. A hold-out test set to measure Cohen's kappa vs the judge
     and vs the v1.1.0 heuristic.
  3. A calibration pass to pick the `prefer_model_above` threshold
     used by the harvester (default 0.6).

What this script DOES do today:
  - Verifies the training prerequisites (transformers, torch, a
    labelled-rollouts JSONL file).
  - Sets up the DistilBERT-for-sequence-classification model with
    3 output classes.
  - Runs ONE demo training step (forward + backward) so we know the
    model architecture loads end-to-end on this machine. Real
    training is gated on a labelled dataset being present.
  - Writes the model + a `skillopt_reward_version.json` stamp to
    `<plugin>/models/reward_model/` - the path the reward_model
    helper looks at on startup.

Usage (full training run, once labels are available):
  python scripts/train_reward_model.py \\
      --labels logs/runs/labels.jsonl \\
      --output models/reward_model \\
      --epochs 3 \\
      --batch-size 16

Run without args to execute the prerequisite + smoke-step path
(this is what the v1.2.0 smoke tests invoke to prove the training
script is wired correctly).

Failure-mode policy (per ROADMAP engineering principle 3):
  - Missing dependencies -> clear error, exit 2.
  - Missing labels file -> clear error, exit 3.
  - Training exception -> write a partial checkpoint if possible,
    exit 4. Never silently lose work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


PLUGIN_NAME = "skillopt"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _check_deps() -> tuple[bool, str]:
    try:
        import torch  # type: ignore  # noqa: F401
        from transformers import (  # type: ignore  # noqa: F401
            AutoModelForSequenceClassification,
            AutoTokenizer,
            DistilBertForSequenceClassification,
            DistilBertTokenizerFast,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, "ok"


def _build_model(num_labels: int = 3):
    """Construct a fresh DistilBERT classifier with 3 output classes."""
    from transformers import (  # type: ignore
        DistilBertForSequenceClassification,
        DistilBertTokenizerFast,
    )
    tokenizer = DistilBertTokenizerFast.from_pretrained("distilbert-base-uncased")
    model = DistilBertForSequenceClassification.from_pretrained(
        "distilbert-base-uncased",
        num_labels=num_labels,
    )
    return tokenizer, model


def _smoke_step(tokenizer, model, text: str, label: int) -> tuple[bool, str]:
    """Run one forward+backward pass on a single example. Proves the
    training loop wires up end-to-end without doing a real fit."""
    try:
        import torch  # type: ignore
        enc = tokenizer(text, return_tensors="pt", truncation=True, max_length=128)
        out = model(**enc, labels=torch.tensor([label]))
        out.loss.backward()
        return True, f"loss={out.loss.item():.4f}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _write_version_stamp(out_dir: Path, *, label_count: int, epochs: int) -> None:
    stamp = {
        "version": f"1.2.0-smoke-{int(time.time())}",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label_count": label_count,
        "epochs": epochs,
        "schema": "distilbert-base-uncased + 3-class classifier head",
        "labels": ["success", "partial", "failure"],
    }
    (out_dir / "skillopt_reward_version.json").write_text(
        json.dumps(stamp, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cmd_smoke(args: argparse.Namespace) -> int:
    """Verify the training pipeline wires up. Used by smoke tests."""
    print(f"[{PLUGIN_NAME}] train_reward_model.py smoke step")
    print(f"[{PLUGIN_NAME}] plugin_root = {_plugin_root()}")

    ok, info = _check_deps()
    if not ok:
        print(f"[{PLUGIN_NAME}] ERROR: training deps missing: {info}")
        print(f"[{PLUGIN_NAME}] pip install 'transformers[torch]' into the A0 venv")
        return 2
    print(f"[{PLUGIN_NAME}] OK: training deps present ({info})")

    if args.labels and not Path(args.labels).is_file():
        print(f"[{PLUGIN_NAME}] ERROR: --labels file not found: {args.labels}")
        return 3
    label_count = 0
    if args.labels:
        with open(args.labels, encoding="utf-8") as f:
            label_count = sum(1 for ln in f if ln.strip())
        print(f"[{PLUGIN_NAME}] OK: labels file has {label_count} rows")
    else:
        print(f"[{PLUGIN_NAME}] NOTE: no --labels given, will run smoke step only")

    out_dir = Path(args.output) if args.output else (_plugin_root() / "models" / "reward_model")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{PLUGIN_NAME}] output dir = {out_dir}")

    print(f"[{PLUGIN_NAME}] building model (distilbert-base-uncased + 3 classes)...")
    try:
        tokenizer, model = _build_model(num_labels=3)
    except Exception as e:
        print(f"[{PLUGIN_NAME}] ERROR: model build failed: {type(e).__name__}: {e}")
        return 4
    print(f"[{PLUGIN_NAME}] OK: model built (params ~66M base + 1.5K head)")

    # Demo training step. Proves the forward + backward path works.
    sample = "Refactored the parser to handle nested groups. Tests pass."
    ok, info = _smoke_step(tokenizer, model, sample, label=0)
    if not ok:
        print(f"[{PLUGIN_NAME}] ERROR: smoke step failed: {info}")
        return 5
    print(f"[{PLUGIN_NAME}] OK: smoke step ran ({info})")

    if args.persist:
        print(f"[{PLUGIN_NAME}] persisting model + version stamp to {out_dir} ...")
        try:
            tokenizer.save_pretrained(str(out_dir))
            model.save_pretrained(str(out_dir))
            _write_version_stamp(out_dir, label_count=label_count, epochs=args.epochs)
        except Exception as e:
            print(f"[{PLUGIN_NAME}] ERROR: persist failed: {type(e).__name__}: {e}")
            return 6
        print(f"[{PLUGIN_NAME}] OK: model persisted. Run helpers/reward_model.py to verify.")
    else:
        print(f"[{PLUGIN_NAME}] NOTE: --persist not set; model not written to disk.")
        print(f"[{PLUGIN_NAME}]   pass --persist to drop a trained model into {out_dir}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Train the SkillOpt reward model (DistilBERT, 3 classes).",
    )
    p.add_argument(
        "--labels", type=str, default=None,
        help="Path to a JSONL file with one labelled rollout per line. "
             "Required for a real training run; optional for the smoke step.",
    )
    p.add_argument(
        "--output", type=str, default=None,
        help="Where to write the trained model. Default: <plugin>/models/reward_model",
    )
    p.add_argument(
        "--epochs", type=int, default=3,
        help="Number of training epochs (default 3).",
    )
    p.add_argument(
        "--batch-size", type=int, default=16,
        help="Per-device batch size (default 16).",
    )
    p.add_argument(
        "--persist", action="store_true",
        help="Actually write the model + version stamp to the output dir.",
    )
    args = p.parse_args(argv)
    return cmd_smoke(args)


if __name__ == "__main__":
    sys.exit(main())
