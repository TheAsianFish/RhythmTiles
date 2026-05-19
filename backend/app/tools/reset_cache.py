"""Reset cache entries for a single pipeline mode.

Usage:
    python -m app.tools.reset_cache MODE [--dry-run]

MODE is one of:
    baseline   bt=0 dm=0 mt=0   (librosa heuristic)
    light      bt=1 dm=0 mt=0   (Beat This!)
    full       bt=1 dm=1 mt=0   (+ Demucs)
    max        bt=1 dm=1 mt=1   (+ MERT)
    all                          (every chart row + every cache file)

Scope rules (default, no flags):
    - Always: chart_cache rows whose mode_key matches the target mode.
    - max: also clears MERT section cache (exclusive to ML-max).
    - light / full: clears only chart rows. Beat cache and per-stem
      onset cache are shared with other ML modes, so wiping them here
      would silently invalidate cached work owned by another mode.

Cache layout (for reference):
    cache/charts.sqlite              chart_cache rows keyed by "<videoId>|<mode_key>"
    cache/beats/<hash>-beat-this.json shared by light/full/max
    cache/onsets/<hash>-per-stem.json shared by full/max
    cache/sections/<hash>-mert.json  exclusive to max
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.config import settings
from app.models import PIPELINE_VERSION


MODES = {
    "baseline": "bt0-dm0-mt0",
    "light":    "bt1-dm0-mt0",
    "full":     "bt1-dm1-mt0",
    "max":      "bt1-dm1-mt1",
}


def _major_minor() -> str:
    return ".".join(PIPELINE_VERSION.split(".")[:2])


def _current_mode_suffix(mode: str) -> str:
    """The exact mode_key the running server would write today."""
    return f"v{_major_minor()}-{MODES[mode]}"


def _legacy_suffix_pattern(mode: str) -> str:
    """SQL LIKE pattern matching ANY pipeline version with this mode's flags.

    Caches accumulate across version bumps (v0.9 rows are still present
    alongside v0.10). For a "reset mode X" to actually clear it, we need
    to match every version, not just the current one.
    """
    return f"%-{MODES[mode]}"


def _summarize_chart_rows(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return conn.execute(
        "SELECT substr(content_hash, instr(content_hash,'|')+1) AS mode, COUNT(*) "
        "FROM chart_cache GROUP BY mode ORDER BY mode"
    ).fetchall()


def _delete_chart_rows(conn: sqlite3.Connection, mode: str, dry_run: bool) -> int:
    if mode == "all":
        sql = "SELECT COUNT(*) FROM chart_cache"
        n = conn.execute(sql).fetchone()[0]
        if not dry_run:
            conn.execute("DELETE FROM chart_cache")
            conn.commit()
        return int(n)

    pattern = _legacy_suffix_pattern(mode)
    n = conn.execute(
        "SELECT COUNT(*) FROM chart_cache WHERE content_hash LIKE ?",
        (pattern,),
    ).fetchone()[0]
    if not dry_run:
        conn.execute(
            "DELETE FROM chart_cache WHERE content_hash LIKE ?",
            (pattern,),
        )
        conn.commit()
    return int(n)


def _exclusive_files_for(mode: str) -> list[Path]:
    """Return cache files that ONLY this mode produces; safe to delete."""
    if mode == "max":
        return sorted((settings.cache_dir / "sections").glob("*-mert.json"))
    if mode == "all":
        out: list[Path] = []
        for sub in ("beats", "onsets", "sections", "audio"):
            d = settings.cache_dir / sub
            if d.exists():
                out.extend(sorted(d.iterdir()))
        return out
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset cache for one pipeline mode.")
    parser.add_argument(
        "mode",
        choices=[*MODES.keys(), "all"],
        help="Which mode's cache to reset.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted; do not modify anything.",
    )
    args = parser.parse_args()

    db_path = settings.cache_dir / "charts.sqlite"
    if not db_path.exists():
        print(f"no cache db at {db_path}; nothing to reset", file=sys.stderr)
        return 0

    conn = sqlite3.connect(db_path)

    print(f"target mode: {args.mode}")
    if args.mode != "all":
        print(f"current write key: {_current_mode_suffix(args.mode)}")
        print(f"match pattern: {_legacy_suffix_pattern(args.mode)}")

    print("\nchart_cache rows by mode BEFORE:")
    for mode, n in _summarize_chart_rows(conn):
        marker = " <- target" if args.mode == "all" or mode.endswith(MODES.get(args.mode, "")) else ""
        print(f"  {mode}: {n}{marker}")

    rows = _delete_chart_rows(conn, args.mode, args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"\n{verb} {rows} chart_cache rows")

    files = _exclusive_files_for(args.mode)
    file_count = 0
    for f in files:
        if not args.dry_run:
            try:
                f.unlink()
            except OSError as exc:
                print(f"  could not remove {f.name}: {exc}", file=sys.stderr)
                continue
        file_count += 1
    if files:
        print(f"{verb} {file_count} exclusive cache files ({files[0].parent.name}/)")

    if not args.dry_run:
        print("\nchart_cache rows by mode AFTER:")
        for mode, n in _summarize_chart_rows(conn):
            print(f"  {mode}: {n}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
