"""One-shot training run. Pulls charts, extracts features, trains v0.2.

Designed for "leave the window open and walk away" UX. Resumable: if you
cancel or crash mid-way, re-running picks up where the manifest + shards
left off.

Run with:
    .\\.venv\\Scripts\\python.exe -m app.training.run_overnight

Default settings (tuned for a ~2-3 hour CPU run without Demucs):
    --target 500            charts to pull
    --no-auth               use nerinyan mirror, no OAuth
    --use-beat-this         richer beat + downbeat features
    --use-mert              section bucket features
    (--use-demucs OFF)      Demucs is the slow stage; skip for v0.2

Output:
    cache/training/manifest.json
    cache/training/osz/*.osz       (~5GB at default target)
    cache/training/osu/*.osu       (chart files)
    cache/training/features/*.parquet  (one per chart)
    backend/models/lane_v1/         (model overwrites v0.1)
    run_overnight.log               (tail this for live progress)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def _setup_logging(log_path: Path) -> logging.Logger:
    """Log to BOTH stdout AND a file so a long run survives a closed window."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, mode="a", encoding="utf-8"),
    ]
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("beatbridge.training.run_overnight")


def _section(logger: logging.Logger, title: str) -> None:
    logger.info("=" * 72)
    logger.info(title)
    logger.info("=" * 72)


def _free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return -1.0


# Windows SetThreadExecutionState flags. Tells the OS "don't sleep while
# this process is running." Released automatically when the process exits.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


def _prevent_sleep(logger: logging.Logger) -> bool:
    """Ask Windows to keep the system awake. Returns True on success.

    No-op on non-Windows. Failure is non-fatal; we log and continue. The
    flag is process-scoped: as soon as this Python interpreter exits, the
    sleep policy reverts to normal.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes  # noqa: WPS433
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED,
        )
        # SetThreadExecutionState returns 0 on failure.
        if result == 0:
            logger.warning("SetThreadExecutionState returned 0; sleep prevention may not be active")
            return False
        logger.info("sleep prevention: ENABLED (process-scoped; reverts on exit)")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not prevent sleep (continuing anyway): %s", exc)
        return False


def _allow_sleep() -> None:
    """Restore default sleep behaviour. Called from the exit handler."""
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: WPS433
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", type=int, default=1000,
        help=(
            "Total charts to train on. Default 1000 (~10GB disk). "
            "Smaller (e.g. 200) iterates faster; larger (e.g. 2000) "
            "polishes harder but with diminishing returns."
        ),
    )
    parser.add_argument(
        "--training-dir", type=Path, default=Path("cache/training"),
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("../backend/models/lane_v1"),
        help="Where to save the trained model artifact.",
    )
    # Tri-state Demucs flag: --use-demucs forces on, --no-demucs forces off,
    # and absence of both means "auto" (GPU -> on, CPU -> off). Demucs is
    # ~30x faster on a consumer GPU than CPU, so the right default depends
    # on hardware.
    demucs_group = parser.add_mutually_exclusive_group()
    demucs_group.add_argument(
        "--use-demucs", action="store_true",
        help="Force Demucs ON regardless of GPU availability.",
    )
    demucs_group.add_argument(
        "--no-demucs", action="store_true",
        help="Force Demucs OFF regardless of GPU availability.",
    )
    parser.add_argument(
        "--skip-collect", action="store_true",
        help="Skip the collection step (use existing manifest as-is).",
    )
    parser.add_argument(
        "--skip-extract", action="store_true",
        help="Skip the feature-extraction step.",
    )
    parser.add_argument(
        "--skip-train", action="store_true",
        help="Skip training (just collect + extract).",
    )
    parser.add_argument(
        "--force-extract", action="store_true",
        help=(
            "Re-extract feature shards even when they already exist. Use this "
            "after a feature-code bugfix to overwrite poisoned shards."
        ),
    )
    args = parser.parse_args()

    log_path = Path("run_overnight.log")
    logger = _setup_logging(log_path)

    # Best-effort keep the system awake for the duration of this process.
    # Released automatically on exit (even on Ctrl+C or crash).
    import atexit  # noqa: WPS433

    _prevent_sleep(logger)
    atexit.register(_allow_sleep)

    # Decide whether to use Demucs. Auto-on when a CUDA GPU is visible;
    # auto-off on pure CPU. Either flag overrides the auto decision.
    has_gpu, gpu_label = _detect_gpu()
    if args.use_demucs:
        use_demucs = True
        demucs_reason = "forced ON via --use-demucs"
    elif args.no_demucs:
        use_demucs = False
        demucs_reason = "forced OFF via --no-demucs"
    else:
        use_demucs = has_gpu
        demucs_reason = (
            f"auto (GPU detected: {gpu_label})" if has_gpu else "auto (no GPU; CPU is too slow)"
        )

    started_at = datetime.now()
    _section(logger, "BeatBridge Phase 5 - one-shot training run")
    logger.info("started at: %s", started_at.isoformat(timespec="seconds"))
    logger.info("target charts: %d", args.target)
    logger.info("training dir: %s (free disk: %.1f GB)",
                args.training_dir, _free_disk_gb(args.training_dir.parent or Path(".")))
    logger.info("model dir:    %s", args.out_dir)
    logger.info("gpu:          %s", gpu_label or "none (CPU-only)")
    logger.info("use_demucs:   %s (%s)", use_demucs, demucs_reason)
    logger.info("log file:     %s", log_path.resolve())
    logger.info("")
    logger.info("Estimated time: %s", _estimate_runtime(
        target=args.target, use_demucs=use_demucs, has_gpu=has_gpu,
    ))
    logger.info("You can close this window safely once it's running if you")
    logger.info("invoked it inside a background-tolerant shell. The log file")
    logger.info("will keep updating. To monitor live, in another window:")
    logger.info("    Get-Content run_overnight.log -Wait -Tail 30")
    logger.info("")

    # Defer the heavy imports until after logging is set up; if one of these
    # fails, the user gets a clear error in the log.
    try:
        from app.training.collect import collect
        from app.training.train_lane import cmd_extract, cmd_train
    except ImportError as exc:
        logger.exception("import failure; install training deps first: %s", exc)
        return 2

    # --- Stage 1: collect ----------------------------------------------------
    if args.skip_collect:
        _section(logger, "stage 1/3: collect  (SKIPPED via --skip-collect)")
    else:
        _section(logger, "stage 1/3: collect")
        t0 = time.monotonic()
        try:
            manifest = collect(
                target=args.target,
                out_dir=args.training_dir,
                skip_osz=False,
                use_mirror=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("collect failed: %s", exc)
            return 2
        elapsed = time.monotonic() - t0
        logger.info("collect done: %d manifest entries in %.1f min",
                    len(manifest), elapsed / 60)

    # --- Stage 2: feature extraction ----------------------------------------
    if args.skip_extract:
        _section(logger, "stage 2/3: feature extraction  (SKIPPED)")
    else:
        _section(logger, "stage 2/3: feature extraction")
        t0 = time.monotonic()
        ns = argparse.Namespace(
            training_dir=args.training_dir,
            use_demucs=use_demucs,
            use_beat_this=True,
            use_mert=True,
            force=args.force_extract,
        )
        code = cmd_extract(ns)
        elapsed = time.monotonic() - t0
        logger.info("extract done in %.1f min (rc=%d)", elapsed / 60, code)
        if code != 0:
            logger.warning("extract returned non-zero; continuing to train anyway")

    # --- Stage 3: training --------------------------------------------------
    if args.skip_train:
        _section(logger, "stage 3/3: training  (SKIPPED)")
    else:
        _section(logger, "stage 3/3: training")
        t0 = time.monotonic()
        ns = argparse.Namespace(
            training_dir=args.training_dir,
            out_dir=args.out_dir,
            num_rounds=500,
        )
        code = cmd_train(ns)
        elapsed = time.monotonic() - t0
        logger.info("train done in %.1f min (rc=%d)", elapsed / 60, code)
        if code != 0:
            logger.error("training failed; check log above for cause")
            return code

    finished_at = datetime.now()
    total = finished_at - started_at
    _section(logger, "DONE")
    logger.info("finished at: %s", finished_at.isoformat(timespec="seconds"))
    logger.info("total wall time: %s", _fmt_td(total))
    logger.info("")
    logger.info("Model artifact: %s", args.out_dir)
    logger.info("To activate in the backend, set USE_LEARNED_LANES=1 and restart:")
    logger.info("    $env:USE_LEARNED_LANES = '1'")
    logger.info("    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000")
    return 0


def _fmt_td(td: timedelta) -> str:
    total_s = int(td.total_seconds())
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _detect_gpu() -> tuple[bool, str]:
    """Return (has_cuda, human_label). False/'' on import failure or no device."""
    try:
        import torch  # noqa: WPS433
    except Exception:
        return False, ""
    if not torch.cuda.is_available() or torch.cuda.device_count() == 0:
        return False, ""
    p = torch.cuda.get_device_properties(0)
    return True, f"{p.name} ({p.total_memory / 1024**3:.1f} GB)"


def _estimate_runtime(*, target: int, use_demucs: bool, has_gpu: bool) -> str:
    """Rough wall-clock estimate. Calibrated against our actual benchmarks."""
    # Per-song times in seconds, approximate.
    if use_demucs and has_gpu:
        per_song_extract = 22  # Demucs ~15s + Beat This ~0.5s + MERT ~0.5s + librosa ~5s
    elif use_demucs:
        per_song_extract = 360  # CPU Demucs is the killer; ~6 min per song
    elif has_gpu:
        per_song_extract = 9   # Beat This + MERT on GPU; librosa still CPU
    else:
        per_song_extract = 14  # All CPU; librosa + Beat This dominate
    download_s = target * 1.0  # mirror rate-limited to 1/sec
    extract_s = target * per_song_extract
    train_s = 15 * 60  # ~15 min regardless of corpus size at our scale
    total_h = (download_s + extract_s + train_s) / 3600
    return f"~{total_h:.1f} hours ({target} charts; demucs={'on' if use_demucs else 'off'}; gpu={'yes' if has_gpu else 'no'})"


if __name__ == "__main__":
    sys.exit(main())
