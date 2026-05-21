"""v2 lane assigner: small Transformer encoder over onset-feature sequences.

Reads the same parquet shards as the v1 LightGBM trainer (FEATURE_SCHEMA
v2.0, 33 columns). The difference: instead of predicting each event in
isolation, the Transformer sees a window of 32 consecutive onsets and
outputs a per-position 4-class logit. This lets it model sequence-level
patterns like "I picked left-left-left, time for right" that
gradient-boosted trees can't represent.

Architecture (kept small on purpose for fast iteration):
  - Linear projection of 33-dim features -> 192-dim hidden
  - Learned positional embedding (max seq 64)
  - 4 transformer encoder layers, 4 heads, FFN 768
  - Linear head 192 -> 4 (lane logits)
  - ~1.8M params total

Training:
  - Window length 32, stride 16 (50% overlap)
  - Train/val/test split BY BEATMAP_ID (same as v1, no leakage)
  - AdamW + cosine LR schedule
  - Cross-entropy loss per position
  - Early stopping on val accuracy

CLI:
    python -m app.training.lane_transformer train [--epochs 80] [--out-dir ...]
    python -m app.training.lane_transformer evaluate --model-dir ...

The trained artifact lives at backend/models/lane_transformer_v1/ and
contains:
  - model.pt              torch state dict
  - feature_schema.json   column ordering + version stamp (must match runtime)
  - metadata.json         hyperparams + metrics

Inference: see app/ml/lane_transformer_inference.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("beatbridge.training.lane_transformer")

# Where artifacts land.
DEFAULT_MODEL_DIR = Path("../backend/models/lane_transformer_v1")

# Sequence length the model sees at training and inference. 32 events
# covers roughly a phrase at typical pop density (3-5 NPS x 6-10 sec
# context). Bigger = more context but more compute; smaller = less
# context but might miss longer phrasing.
SEQ_LEN = 32
SEQ_STRIDE = 16  # 50% overlap

# Train/val/test split fractions BY BEATMAP_ID. Identical to v1 so the
# split is comparable.
_TRAIN_FRAC = 0.70
_VAL_FRAC = 0.15
_SEED = 20260520


# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------


def _build_model(*, feature_dim: int, hidden: int = 192, n_heads: int = 4,
                 n_layers: int = 4, max_seq: int = 64, n_classes: int = 4):
    """Build the LaneTransformer. Imports torch lazily so the module
    itself loads without torch present."""
    import torch  # noqa: WPS433
    import torch.nn as nn  # noqa: WPS433

    class LaneTransformer(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(feature_dim, hidden)
            self.pos_emb = nn.Embedding(max_seq, hidden)
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=hidden,
                nhead=n_heads,
                dim_feedforward=hidden * 4,
                dropout=0.1,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
            self.norm = nn.LayerNorm(hidden)
            self.head = nn.Linear(hidden, n_classes)

        def forward(self, x):  # x: (B, T, F)
            h = self.input_proj(x)
            pos_ids = torch.arange(x.size(1), device=x.device)
            h = h + self.pos_emb(pos_ids).unsqueeze(0)
            h = self.encoder(h)
            h = self.norm(h)
            return self.head(h)  # (B, T, n_classes)

    return LaneTransformer()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_shards_grouped(features_dir: Path):
    """Yield (beatmap_id, X, y) per parquet shard.

    X is shape (N, F), y is shape (N,) of ints in [0, 4).
    """
    import numpy as np  # noqa: WPS433
    import pyarrow.parquet as pq  # noqa: WPS433

    from app.training.features import ALL_FEATURE_COLUMNS

    feature_cols = list(ALL_FEATURE_COLUMNS)
    for shard in sorted(features_dir.glob("*.parquet")):
        bid = int(shard.stem)
        try:
            table = pq.read_table(shard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shard %s read failed (skipping): %s", shard.name, exc)
            continue
        cols = [
            np.asarray(table.column(c).to_numpy(), dtype=np.float32)
            for c in feature_cols
        ]
        X = np.stack(cols, axis=1)
        y = np.asarray(table.column("lane").to_numpy(), dtype=np.int64)
        if len(y) == 0:
            continue
        yield bid, X, y


def _window_indices(n: int, *, seq_len: int = SEQ_LEN, stride: int = SEQ_STRIDE):
    """Yield (start, end) windows. Final window is right-aligned so we don't
    drop the tail of every song."""
    if n < seq_len:
        # Pad? We'll just skip very short songs (< 32 events). They're rare.
        return
    for start in range(0, n - seq_len + 1, stride):
        yield start, start + seq_len
    # Right-align the last window if the stride didn't reach the end.
    last_start = ((n - seq_len) // stride) * stride
    if last_start + seq_len < n:
        yield n - seq_len, n


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


@dataclass
class TrainingMetrics:
    train_loss: float
    val_loss: float
    train_accuracy: float
    val_accuracy: float
    test_accuracy: float
    test_lane_distribution: dict
    test_per_lane_accuracy: dict
    n_train_windows: int
    n_val_windows: int
    n_test_windows: int
    n_beatmaps_train: int
    n_beatmaps_val: int
    n_beatmaps_test: int
    best_epoch: int
    feature_dim: int


def cmd_train(args: argparse.Namespace) -> int:
    """Train the lane transformer."""
    try:
        import numpy as np  # noqa: WPS433
        import torch  # noqa: WPS433
        from torch import nn  # noqa: WPS433
        from torch.utils.data import DataLoader, TensorDataset  # noqa: WPS433
    except ImportError as exc:
        print(f"FATAL: training requires torch + numpy. {exc}", file=sys.stderr)
        return 2

    from app.training.features import ALL_FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION

    features_dir = args.training_dir / "features"
    if not features_dir.exists():
        print(f"no features dir at {features_dir}", file=sys.stderr)
        return 2

    print(f"loading shards from {features_dir}...")
    by_beatmap: dict[int, tuple] = {}
    for bid, X, y in _load_shards_grouped(features_dir):
        by_beatmap[bid] = (X, y)
    if not by_beatmap:
        print("no shards loaded", file=sys.stderr)
        return 2
    print(f"  loaded {len(by_beatmap)} beatmaps")

    rng = np.random.default_rng(_SEED)
    ids = np.array(sorted(by_beatmap.keys()))
    rng.shuffle(ids)
    n_total = len(ids)
    n_train = int(round(n_total * _TRAIN_FRAC))
    n_val = int(round(n_total * _VAL_FRAC))
    train_ids = set(int(x) for x in ids[:n_train])
    val_ids = set(int(x) for x in ids[n_train:n_train + n_val])
    test_ids = set(int(x) for x in ids[n_train + n_val:])

    def windows_for(target_ids: set[int]) -> tuple:
        """Stack windows across all beatmaps in `target_ids`. Returns
        (X_windows, y_windows) of shapes (W, seq_len, F) and (W, seq_len)."""
        xw_list = []
        yw_list = []
        for bid in target_ids:
            X, y = by_beatmap[bid]
            for start, end in _window_indices(len(y)):
                xw_list.append(X[start:end])
                yw_list.append(y[start:end])
        if not xw_list:
            return (
                np.zeros((0, SEQ_LEN, len(ALL_FEATURE_COLUMNS)), dtype=np.float32),
                np.zeros((0, SEQ_LEN), dtype=np.int64),
            )
        return np.stack(xw_list, axis=0), np.stack(yw_list, axis=0)

    X_train, y_train = windows_for(train_ids)
    X_val, y_val = windows_for(val_ids)
    X_test, y_test = windows_for(test_ids)

    feature_dim = X_train.shape[-1]
    print(
        f"split: train={len(train_ids)} beatmaps / {len(X_train)} windows, "
        f"val={len(val_ids)} / {len(X_val)}, "
        f"test={len(test_ids)} / {len(X_test)}",
    )
    print(f"feature_dim={feature_dim}, seq_len={SEQ_LEN}")

    # Feature normalisation: standardize per-column using train stats.
    # Transformer is sensitive to feature scale.
    mean = X_train.mean(axis=(0, 1), keepdims=True)
    std = X_train.std(axis=(0, 1), keepdims=True) + 1e-6
    X_train_n = (X_train - mean) / std
    X_val_n = (X_val - mean) / std
    X_test_n = (X_test - mean) / std

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={device}")

    model = _build_model(feature_dim=feature_dim).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"model params: {n_params:,}")

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    loss_fn = nn.CrossEntropyLoss()

    def loader(X, y, *, shuffle: bool):
        ds = TensorDataset(torch.from_numpy(X).float(), torch.from_numpy(y).long())
        return DataLoader(ds, batch_size=args.batch_size, shuffle=shuffle, drop_last=False)

    train_loader = loader(X_train_n, y_train, shuffle=True)
    val_loader = loader(X_val_n, y_val, shuffle=False)
    test_loader = loader(X_test_n, y_test, shuffle=False)

    def epoch_loop(loader_, train: bool) -> tuple[float, float]:
        if train:
            model.train()
        else:
            model.eval()
        total_loss = 0.0
        total_correct = 0
        total_count = 0
        for xb, yb in loader_:
            xb = xb.to(device)
            yb = yb.to(device)
            if train:
                opt.zero_grad()
            with torch.set_grad_enabled(train):
                logits = model(xb)  # (B, T, 4)
                loss = loss_fn(logits.reshape(-1, 4), yb.reshape(-1))
                if train:
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    opt.step()
            pred = logits.argmax(dim=-1)
            total_correct += (pred == yb).sum().item()
            total_count += yb.numel()
            total_loss += loss.item() * yb.numel()
        return total_loss / max(1, total_count), total_correct / max(1, total_count)

    best_val_acc = -1.0
    best_epoch = -1
    best_state = None
    patience = 10
    bad_epochs = 0
    for epoch in range(args.epochs):
        train_loss, train_acc = epoch_loop(train_loader, train=True)
        val_loss, val_acc = epoch_loop(val_loader, train=False)
        scheduler.step()
        print(
            f"epoch {epoch + 1:3d}/{args.epochs}  "
            f"train_loss={train_loss:.4f} acc={train_acc:.3f}  "
            f"val_loss={val_loss:.4f} acc={val_acc:.3f}",
        )
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_epoch = epoch + 1
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            bad_epochs = 0
        else:
            bad_epochs += 1
            if bad_epochs >= patience:
                print(f"early stop at epoch {epoch + 1}; best epoch={best_epoch}")
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    # Final eval.
    test_loss, test_acc = epoch_loop(test_loader, train=False)
    print(f"\nTEST: loss={test_loss:.4f} acc={test_acc:.3f}")

    # Per-lane accuracy + distribution on test.
    model.eval()
    test_preds = []
    test_truth = []
    with torch.no_grad():
        for xb, yb in test_loader:
            logits = model(xb.to(device))
            test_preds.append(logits.argmax(dim=-1).cpu().numpy())
            test_truth.append(yb.numpy())
    pred_flat = np.concatenate([p.reshape(-1) for p in test_preds])
    true_flat = np.concatenate([t.reshape(-1) for t in test_truth])
    test_lane_dist = {
        int(L): float((pred_flat == L).mean()) for L in range(4)
    }
    test_per_lane = {}
    for L in range(4):
        mask = true_flat == L
        if int(mask.sum()) == 0:
            test_per_lane[int(L)] = 0.0
        else:
            test_per_lane[int(L)] = float((pred_flat[mask] == L).mean())

    metrics = TrainingMetrics(
        train_loss=train_loss,
        val_loss=val_loss,
        train_accuracy=train_acc,
        val_accuracy=best_val_acc,
        test_accuracy=test_acc,
        test_lane_distribution=test_lane_dist,
        test_per_lane_accuracy=test_per_lane,
        n_train_windows=int(len(X_train)),
        n_val_windows=int(len(X_val)),
        n_test_windows=int(len(X_test)),
        n_beatmaps_train=len(train_ids),
        n_beatmaps_val=len(val_ids),
        n_beatmaps_test=len(test_ids),
        best_epoch=best_epoch,
        feature_dim=feature_dim,
    )

    print("\n=== METRICS ===")
    print(json.dumps(asdict(metrics), indent=2))

    # Save artifacts.
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "feature_dim": feature_dim,
            "mean": mean.squeeze().astype("float32").tolist(),
            "std": std.squeeze().astype("float32").tolist(),
            "seq_len": SEQ_LEN,
            "hidden": 192,
            "n_heads": 4,
            "n_layers": 4,
            "max_seq": 64,
            "n_classes": 4,
        },
        str(out_dir / "model.pt"),
    )
    (out_dir / "feature_schema.json").write_text(
        json.dumps(
            {
                "version": FEATURE_SCHEMA_VERSION,
                "columns": list(ALL_FEATURE_COLUMNS),
                "label_column": "lane",
                "num_classes": 4,
                "architecture": "transformer_v1",
                "seq_len": SEQ_LEN,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "metadata.json").write_text(
        json.dumps(
            {
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "seed": _SEED,
                "epochs_run": best_epoch,
                "max_epochs": args.epochs,
                "batch_size": args.batch_size,
                "lr": args.lr,
                "metrics": asdict(metrics),
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "architecture": "transformer_v1",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nsaved model to {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_train = sub.add_parser("train", help="train the lane transformer")
    p_train.add_argument("--training-dir", type=Path, default=Path("cache/training"))
    p_train.add_argument("--out-dir", type=Path, default=DEFAULT_MODEL_DIR)
    p_train.add_argument("--epochs", type=int, default=80)
    p_train.add_argument("--batch-size", type=int, default=128)
    p_train.add_argument("--lr", type=float, default=3e-4)
    p_train.set_defaults(func=cmd_train)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(_cli())
