#!/usr/bin/env python3
"""
SkillOpt reward model training script - v1.8.0.

ROADMAP Day-3, item 3 (reward model).

This script trains a 3-class DistilBERT classifier (success / partial /
failure) on labelled agent rollouts. The model is then used by
`helpers/reward_model.py` to score every new rollout produced by the
harvester.

Two modes (``--mode``):

- **smoke** (default, backwards compatible with v1.2.0): verifies the
  training prerequisites (transformers, torch), builds the DistilBERT
  classifier, runs ONE demo forward+backward step so we know the
  architecture loads end-to-end, and optionally persists the untrained
  model + a ``skillopt_reward_version.json`` stamp (``1.2.0-smoke-*``).
  This is what the smoke tests invoke.
- **train** (v1.8.0): the real training loop. Loads judge-labelled
  rollouts (``judge_label`` written by ``helpers/llm_judge.py``, P4),
  featurizes them with ``reward_model._rollout_to_text`` (the SAME
  featurizer inference uses), splits train/val deterministically
  (stratified per-class), runs an AdamW epochs loop, evaluates val
  accuracy+loss each epoch, persists the model, and writes a
  ``1.3.0-train-*`` version stamp. The calibration pass that picks
  ``prefer_model_above`` is wired in P6 (``reward_model._calibrate``).

Usage:
  python scripts/train_reward_model.py                          # smoke
  python scripts/train_reward_model.py --mode train \\
      --rollouts-dir logs/rollouts --epochs 3 --batch-size 16

Run without args (or ``--mode smoke``) to execute the prerequisite +
smoke-step path (what the smoke tests invoke to prove the training
script is wired correctly).

Failure-mode policy (per ROADMAP engineering principle 3):
  - Missing dependencies -> clear error, exit 2.
  - Missing / too-few labelled rollouts -> clear error, exit 3.
  - Model build exception -> exit 4. Never silently lose work.
  - Smoke step failure -> exit 5.
  - Persist failure -> exit 6.
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


def _write_version_stamp(
    out_dir: Path, *, label_count: int, epochs: int,
    version_prefix: str = "1.2.0-smoke",
    extra: dict | None = None,
) -> None:
    stamp = {
        "version": f"{version_prefix}-{int(time.time())}",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label_count": label_count,
        "epochs": epochs,
        "schema": "distilbert-base-uncased + 3-class classifier head",
        "labels": ["success", "partial", "failure"],
    }
    if extra:
        stamp.update(extra)
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


# ----------------------------------------------------------------------- #
# v1.8.0 (P5): real training loop
# ----------------------------------------------------------------------- #

# Class index — MUST match reward_model._CLASSES ("success","partial","failure")
# so the trained model's argmax maps to the same labels the judge produces.
_LABEL_IDX = {"success": 0, "partial": 1, "failure": 2}


def _load_labelled_rollouts(rollouts_dir: Path) -> list[tuple[str, int]]:
    """Load (text, label_idx) pairs from a rollouts directory.

    Only rollouts with a `judge_label` in {success,partial,failure} (written
    by helpers/llm_judge.py, P4) are kept; unlabelled rollouts are skipped
    (counted in the returned log, not returned). The text is built with
    `reward_model._rollout_to_text` — the SAME featurizer inference uses —
    so train and inference features match exactly. Changing either side
    without the other silently corrupts the model.
    """
    sys.path.insert(0, str(_plugin_root()))
    from helpers import reward_model  # type: ignore

    samples: list[tuple[str, int]] = []
    n_seen = n_skipped = 0
    for p in sorted(rollouts_dir.glob("*.json")):
        if not p.is_file():
            continue
        n_seen += 1
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            n_skipped += 1
            continue
        label = rec.get("judge_label")
        if label not in _LABEL_IDX:
            n_skipped += 1
            continue
        text = reward_model._rollout_to_text(rec)
        if not text.strip():
            n_skipped += 1
            continue
        samples.append((text, _LABEL_IDX[label]))
    print(f"[{PLUGIN_NAME}] rollouts: seen={n_seen} labelled={len(samples)} skipped={n_skipped}")
    return samples


def _train_val_split(
    samples: list[tuple[str, int]], *, val_frac: float = 0.2
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Deterministic stratified split (per-class every-Nth into val).

    No randomness — reproducible across runs and safe for tests. For each
    class, every val_mod-th sample (in file order) goes to val, the rest
    to train, where val_mod = max(1, round(1/val_frac)). With the default
    val_frac=0.2 that is every 5th per class -> ~20% val. Classes with
    fewer than 2 samples contribute nothing to val (kept in train) so a
    tiny class never loses all its training examples.
    """
    val_mod = max(1, round(1.0 / max(val_frac, 1e-6)))
    train: list[tuple[str, int]] = []
    val: list[tuple[str, int]] = []
    counters: dict[int, int] = {}
    for text, idx in samples:
        c = counters.get(idx, 0)
        counters[idx] = c + 1
        # stratify: a class needs >=2 examples before we peel one off to val
        if c % val_mod == 0 and c > 0:
            # only val if the class has at least one other example for train
            # (checked after the full pass would be complex; instead require
            # c >= 1 which is guaranteed here since c>0 means this is at least
            # the (c+1)-th example). Keep it simple: send to val.
            val.append((text, idx))
        else:
            train.append((text, idx))
    # Guard: if val ended up empty (very small data), pull the last train
    # example of the largest class into val so we always have a val set
    # when there are >=2 samples.
    if not val and len(train) >= 2:
        val.append(train.pop())
    return train, val


def _evaluate(tokenizer, model, batch: list[tuple[str, int]]) -> tuple[float, float]:
    """Return (accuracy, mean_loss) over a batch (no grad). 0/0 when empty."""
    import torch  # type: ignore
    if not batch:
        return 0.0, 0.0
    texts = [t for t, _ in batch]
    labels = [l for _, l in batch]
    enc = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.no_grad():
        out = model(**enc, labels=torch.tensor(labels))
    preds = out.logits.argmax(dim=-1)
    acc = (preds == torch.tensor(labels)).float().mean().item()
    return float(acc), float(out.loss.item())


def cmd_train(args: argparse.Namespace) -> int:
    """Real training loop (v1.8.0, P5). Loads judge-labelled rollouts,
    trains a 3-class DistilBERT classifier, persists it, and writes a
    version stamp. Calibration is wired in P6."""
    print(f"[{PLUGIN_NAME}] train_reward_model.py TRAIN mode (v1.8.0)")
    print(f"[{PLUGIN_NAME}] plugin_root = {_plugin_root()}")

    ok, info = _check_deps()
    if not ok:
        print(f"[{PLUGIN_NAME}] ERROR: training deps missing: {info}")
        print(f"[{PLUGIN_NAME}] pip install 'transformers[torch]' into the A0 venv")
        return 2
    print(f"[{PLUGIN_NAME}] OK: training deps present ({info})")

    rollouts_dir = Path(args.rollouts_dir) if args.rollouts_dir else (_plugin_root() / "logs" / "rollouts")
    if not rollouts_dir.is_dir():
        print(f"[{PLUGIN_NAME}] ERROR: rollouts dir not found: {rollouts_dir}")
        print(f"[{PLUGIN_NAME}] label rollouts first: python scripts/label_rollouts.py")
        return 3

    samples = _load_labelled_rollouts(rollouts_dir)
    min_samples = int(args.min_samples)
    if len(samples) < min_samples:
        print(f"[{PLUGIN_NAME}] ERROR: only {len(samples)} labelled rollouts (need {min_samples})")
        print(f"[{PLUGIN_NAME}] harvest + label more rollouts, or lower --min-samples")
        return 3

    train, val = _train_val_split(samples, val_frac=float(args.val_frac))
    print(f"[{PLUGIN_NAME}] split: train={len(train)} val={len(val)} (val_frac={args.val_frac})")
    if not val:
        print(f"[{PLUGIN_NAME}] WARNING: empty val set — accuracy reporting will be skipped")

    out_dir = Path(args.output) if args.output else (_plugin_root() / "models" / "reward_model")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{PLUGIN_NAME}] output dir = {out_dir}")

    print(f"[{PLUGIN_NAME}] building model (distilbert-base-uncased + 3 classes)...")
    try:
        tokenizer, model = _build_model(num_labels=3)
    except Exception as e:
        print(f"[{PLUGIN_NAME}] ERROR: model build failed: {type(e).__name__}: {e}")
        return 4

    import torch  # type: ignore
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(args.lr))
    batch_size = int(args.batch_size)

    for epoch in range(int(args.epochs)):
        model.train()
        total_loss = 0.0
        n_batches = 0
        for i in range(0, len(train), batch_size):
            batch = train[i:i + batch_size]
            texts = [t for t, _ in batch]
            labels = [l for _, l in batch]
            enc = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
            optimizer.zero_grad()
            out = model(**enc, labels=torch.tensor(labels))
            out.loss.backward()
            optimizer.step()
            total_loss += out.loss.item()
            n_batches += 1
        train_loss = total_loss / max(n_batches, 1)
        if val:
            model.eval()
            # evaluate val in chunks of batch_size
            v_acc = v_loss = v_n = 0.0
            for j in range(0, len(val), batch_size):
                b = val[j:j + batch_size]
                a, l = _evaluate(tokenizer, model, b)
                v_acc += a * len(b); v_loss += l * len(b); v_n += len(b)
            v_acc /= max(v_n, 1); v_loss /= max(v_n, 1)
            print(f"[{PLUGIN_NAME}] epoch {epoch+1}/{args.epochs}: train_loss={train_loss:.4f} val_acc={v_acc:.3f} val_loss={v_loss:.4f}")
        else:
            print(f"[{PLUGIN_NAME}] epoch {epoch+1}/{args.epochs}: train_loss={train_loss:.4f} (no val)")

    print(f"[{PLUGIN_NAME}] persisting model to {out_dir} ...")
    try:
        tokenizer.save_pretrained(str(out_dir))
        model.save_pretrained(str(out_dir))
    except Exception as e:
        print(f"[{PLUGIN_NAME}] ERROR: persist failed: {type(e).__name__}: {e}")
        return 6

    # P6: calibration pass — pick prefer_model_above on the val set and
    # write calibration.json next to the model. Surface T* in the version
    # stamp so the loaded model's threshold is auditable.
    cal_extra: dict = {}
    try:
        sys.path.insert(0, str(_plugin_root()))
        from helpers import reward_model  # type: ignore

        def _probs_fn(text: str) -> list[float]:
            import torch  # type: ignore
            enc = tokenizer(text, return_tensors="pt", truncation=True, max_length=512, padding=False)
            with torch.no_grad():
                logits = model(**enc).logits
            return [float(p) for p in torch.softmax(logits, dim=-1)[0].tolist()]

        cal = reward_model._calibrate(
            None, None, val, probs_fn=_probs_fn, out_dir=out_dir,
        )
        cal_extra = {"prefer_model_above": cal.get("prefer_model_above"),
                     "val_agreement": cal.get("val_agreement")}
        print(f"[{PLUGIN_NAME}] calibration: prefer_model_above={cal.get('prefer_model_above')} "
              f"val_agreement={cal.get('val_agreement')} (n_val={cal.get('n_val')})")
    except Exception as e:
        print(f"[{PLUGIN_NAME}] WARNING: calibration failed: {type(e).__name__}: {e}")

    try:
        _write_version_stamp(
            out_dir, label_count=len(samples), epochs=int(args.epochs),
            version_prefix="1.3.0-train", extra=cal_extra,
        )
    except Exception as e:
        print(f"[{PLUGIN_NAME}] ERROR: version stamp failed: {type(e).__name__}: {e}")
        return 6

    print(f"[{PLUGIN_NAME}] OK: model persisted ({len(samples)} labels, {args.epochs} epochs).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Train the SkillOpt reward model (DistilBERT, 3 classes).",
    )
    p.add_argument(
        "--mode", choices=["smoke", "train"], default="smoke",
        help="smoke = one demo step (default, backwards compatible); "
             "train = real training loop over judge-labelled rollouts (v1.8.0).",
    )
    p.add_argument(
        "--labels", type=str, default=None,
        help="Path to a JSONL file with one labelled rollout per line. "
             "Used by the smoke step only (line count). Optional.",
    )
    p.add_argument(
        "--rollouts-dir", type=str, default=None,
        help="Directory of harvested rollout JSON files to train on (train mode). "
             "Default: <plugin>/logs/rollouts. Rollouts need a judge_label (P4).",
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
        "--min-samples", type=int, default=50,
        help="Minimum labelled rollouts to start training (default 50).",
    )
    p.add_argument(
        "--lr", type=float, default=2e-5,
        help="AdamW learning rate (default 2e-5).",
    )
    p.add_argument(
        "--val-frac", type=float, default=0.2,
        help="Validation split fraction (default 0.2, deterministic stratified).",
    )
    p.add_argument(
        "--persist", action="store_true",
        help="Actually write the model + version stamp to the output dir.",
    )
    args = p.parse_args(argv)
    if args.mode == "train":
        return cmd_train(args)
    return cmd_smoke(args)


if __name__ == "__main__":
    sys.exit(main())
