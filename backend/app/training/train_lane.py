"""LightGBM lane classifier trainer.

Subcommands:
    extract   Walk the manifest, run feature extraction per beatmap, write
              one parquet shard per beatmap to cache/training/features/.
    train     Concat all feature shards, split by beatmap_id, train a
              LightGBM 4-class softmax model, write
              backend/models/lane_v1/ with model + schema + metadata.
    evaluate  Load a trained model, run on a held-out parquet, print
              top-line accuracy + derived game-feel metrics
              (lane distribution KL, hand-balance, anti-cluster rate).

Why one CLI tool rather than three:
    The three steps share constants (FEATURE_SCHEMA_VERSION, the split
    seed, the eval window) and re-using them across files would invite
    drift. One file with three commands keeps the contract visible.

The training-only deps (lightgbm, pyarrow, scikit-learn) are imported
lazily inside each subcommand so `python -m app.training.train_lane --help`
works on a vanilla install.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("beatbridge.training.train_lane")

# Where trained artifacts go. backend/models/lane_v{N}/ contains:
#   model.lgb              binary LightGBM model
#   feature_schema.json    column ordering + version stamp
#   metadata.json          training run details + metrics
DEFAULT_MODEL_DIR = Path("backend/models/lane_v1")

# Train/val/test split fraction (by beatmap_id, not by row).
_TRAIN_FRAC = 0.70
_VAL_FRAC = 0.15
# test_frac = 1 - train - val = 0.15

# Deterministic seed for the split and model. Bump when you intentionally
# change the experimental setup (don't shadow-bump it across runs).
_SEED = 20260518


# ---------------------------------------------------------------------------
# extract subcommand
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    """Iterate the manifest and write one parquet shard per beatmap."""
    from app.training.collect import _load_manifest
    from app.training.features import (
        extract_features_for_beatmap,
        write_parquet,
    )
    from app.training.osu_parse import parse_osu
    from app.training.osz_extract import OszExtractError, extract_audio

    training_dir = args.training_dir
    manifest = _load_manifest(training_dir / "manifest.json")
    if not manifest:
        print(f"no manifest at {training_dir}/manifest.json", file=sys.stderr)
        return 2

    features_dir = training_dir / "features"
    audio_dir = training_dir / "audio"
    features_dir.mkdir(exist_ok=True)
    audio_dir.mkdir(exist_ok=True)

    n_done = 0
    n_failed = 0
    for entry in manifest:
        shard_path = features_dir / f"{entry.beatmap_id}.parquet"
        if shard_path.exists() and not args.force:
            n_done += 1
            continue
        if entry.osz_path is None:
            logger.info("skip %d: no osz (chart-only entry)", entry.beatmap_id)
            n_failed += 1
            continue

        chart_path = training_dir / entry.chart_path
        osz_path = training_dir / entry.osz_path
        try:
            audio_path = extract_audio(
                osz_path=osz_path,
                chart_path=chart_path,
                out_dir=audio_dir,
            )
        except OszExtractError as exc:
            logger.warning("osz_extract %d failed: %s", entry.beatmap_id, exc)
            n_failed += 1
            continue

        try:
            beatmap = parse_osu(chart_path)
            rows = extract_features_for_beatmap(
                audio_path=audio_path,
                events=beatmap.events,
                use_demucs=args.use_demucs,
                use_beat_this=args.use_beat_this,
                use_mert=args.use_mert,
            )
            write_parquet(rows, shard_path)
        except Exception as exc:  # noqa: BLE001 - report and continue
            logger.exception("feature extract %d failed: %s", entry.beatmap_id, exc)
            n_failed += 1
            continue

        n_done += 1
        if n_done % 25 == 0:
            print(f"  extracted {n_done} / {len(manifest)} (failures: {n_failed})")

    print(f"\nextracted {n_done} shards; {n_failed} failures")
    return 0


# ---------------------------------------------------------------------------
# train subcommand
# ---------------------------------------------------------------------------


@dataclass
class TrainingMetrics:
    train_accuracy: float
    val_accuracy: float
    test_accuracy: float
    test_lane_distribution: dict[int, float]
    test_per_lane_accuracy: dict[int, float]
    n_train_rows: int
    n_val_rows: int
    n_test_rows: int
    n_beatmaps_train: int
    n_beatmaps_val: int
    n_beatmaps_test: int


def cmd_train(args: argparse.Namespace) -> int:
    """Concat shards, split by beatmap_id, train LightGBM, save artifact."""
    try:
        import lightgbm as lgb  # noqa: WPS433
        import numpy as np  # noqa: WPS433
        import pyarrow.parquet as pq  # noqa: WPS433
    except ImportError as exc:
        print(
            f"FATAL: training requires lightgbm + pyarrow + numpy. {exc}",
            file=sys.stderr,
        )
        print(
            "Install with: pip install lightgbm pyarrow",
            file=sys.stderr,
        )
        return 2

    from app.training.features import (
        ALL_FEATURE_COLUMNS,
        FEATURE_SCHEMA_VERSION,
    )

    features_dir = args.training_dir / "features"
    shards = sorted(features_dir.glob("*.parquet"))
    if not shards:
        print(f"no parquet shards at {features_dir}", file=sys.stderr)
        return 2

    print(f"loading {len(shards)} shards from {features_dir}")
    shard_tables: list[tuple[int, "pq.ParquetFile"]] = []
    for shard in shards:
        beatmap_id = int(shard.stem)
        shard_tables.append((beatmap_id, shard))

    # Deterministic split BY BEATMAP_ID (not by row). Notes from the same
    # chart cannot leak between train and val/test.
    rng = np.random.default_rng(_SEED)
    ids = np.array([bid for bid, _ in shard_tables])
    rng.shuffle(ids)
    n_total = len(ids)
    n_train = int(round(n_total * _TRAIN_FRAC))
    n_val = int(round(n_total * _VAL_FRAC))
    train_ids = set(int(x) for x in ids[:n_train])
    val_ids = set(int(x) for x in ids[n_train:n_train + n_val])
    test_ids = set(int(x) for x in ids[n_train + n_val:])

    def collect_rows(target_ids: set[int]) -> tuple[Any, Any]:
        feats: list = []
        labels: list = []
        for bid, shard in shard_tables:
            if bid not in target_ids:
                continue
            table = pq.read_table(shard)
            df_feats = np.stack(
                [np.asarray(table.column(c).to_numpy(), dtype=np.float32) for c in ALL_FEATURE_COLUMNS],
                axis=1,
            )
            df_labels = np.asarray(table.column("lane").to_numpy(), dtype=np.int32)
            feats.append(df_feats)
            labels.append(df_labels)
        if not feats:
            return np.zeros((0, len(ALL_FEATURE_COLUMNS)), dtype=np.float32), np.zeros((0,), dtype=np.int32)
        return np.concatenate(feats, axis=0), np.concatenate(labels, axis=0)

    X_train, y_train = collect_rows(train_ids)
    X_val, y_val = collect_rows(val_ids)
    X_test, y_test = collect_rows(test_ids)

    print(
        f"split: train={len(train_ids)} beatmaps / {len(y_train)} rows, "
        f"val={len(val_ids)} / {len(y_val)}, "
        f"test={len(test_ids)} / {len(y_test)}",
    )

    train_set = lgb.Dataset(
        X_train,
        label=y_train,
        feature_name=list(ALL_FEATURE_COLUMNS),
    )
    val_set = lgb.Dataset(
        X_val,
        label=y_val,
        feature_name=list(ALL_FEATURE_COLUMNS),
        reference=train_set,
    )

    params = {
        "objective": "multiclass",
        "num_class": 4,
        "metric": "multi_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 5,
        "verbose": -1,
        "seed": _SEED,
    }
    model = lgb.train(
        params=params,
        train_set=train_set,
        num_boost_round=args.num_rounds,
        valid_sets=[train_set, val_set],
        valid_names=["train", "val"],
        callbacks=[
            lgb.early_stopping(stopping_rounds=20),
            lgb.log_evaluation(period=25),
        ],
    )

    def top1_accuracy(X, y) -> float:
        if len(y) == 0:
            return 0.0
        preds = model.predict(X)
        pred_lanes = np.asarray(preds).argmax(axis=1)
        return float((pred_lanes == y).mean())

    test_preds = model.predict(X_test)
    test_pred_lanes = np.asarray(test_preds).argmax(axis=1) if len(y_test) > 0 else np.zeros((0,), dtype=int)
    test_lane_dist = {
        int(lane): float((test_pred_lanes == lane).mean()) if len(y_test) > 0 else 0.0
        for lane in range(4)
    }
    test_per_lane = {}
    for lane in range(4):
        mask = y_test == lane
        if int(mask.sum()) == 0:
            test_per_lane[int(lane)] = 0.0
            continue
        test_per_lane[int(lane)] = float((test_pred_lanes[mask] == lane).mean())

    metrics = TrainingMetrics(
        train_accuracy=top1_accuracy(X_train, y_train),
        val_accuracy=top1_accuracy(X_val, y_val),
        test_accuracy=top1_accuracy(X_test, y_test),
        test_lane_distribution=test_lane_dist,
        test_per_lane_accuracy=test_per_lane,
        n_train_rows=int(len(y_train)),
        n_val_rows=int(len(y_val)),
        n_test_rows=int(len(y_test)),
        n_beatmaps_train=int(len(train_ids)),
        n_beatmaps_val=int(len(val_ids)),
        n_beatmaps_test=int(len(test_ids)),
    )

    print("\n=== METRICS ===")
    print(json.dumps(asdict(metrics), indent=2))

    # Save artifacts.
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "model.lgb"))
    (out_dir / "feature_schema.json").write_text(
        json.dumps(
            {
                "version": FEATURE_SCHEMA_VERSION,
                "columns": list(ALL_FEATURE_COLUMNS),
                "label_column": "lane",
                "num_classes": 4,
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
                "params": params,
                "num_rounds_run": int(model.current_iteration()),
                "metrics": asdict(metrics),
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
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

    p_extract = sub.add_parser("extract", help="extract features from .osu + audio")
    p_extract.add_argument(
        "--training-dir", type=Path, default=Path("cache/training"),
    )
    p_extract.add_argument("--use-demucs", action="store_true")
    p_extract.add_argument("--use-beat-this", action="store_true")
    p_extract.add_argument("--use-mert", action="store_true")
    p_extract.add_argument(
        "--force", action="store_true",
        help="Re-extract shards even when the parquet already exists.",
    )
    p_extract.set_defaults(func=cmd_extract)

    p_train = sub.add_parser("train", help="train LightGBM on extracted shards")
    p_train.add_argument(
        "--training-dir", type=Path, default=Path("cache/training"),
    )
    p_train.add_argument(
        "--out-dir", type=Path, default=DEFAULT_MODEL_DIR,
    )
    p_train.add_argument(
        "--num-rounds", type=int, default=500,
    )
    p_train.set_defaults(func=cmd_train)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(_cli())
