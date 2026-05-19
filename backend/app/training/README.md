# Training pipeline (Phase 5)

Offline tooling for training the learned-lane classifier. NOT imported
by the serving code path. See `docs/PHASE5_PLAN.md` for the full plan
and rationale.

## Quick start (one command, ~2-3 hours, walk away)

```powershell
# From backend/ in a PowerShell window:
.\.venv\Scripts\python.exe -m app.training.run_overnight
```

That's it. Pulls 500 charts via the no-auth mirror, extracts features
(librosa + Beat This! + MERT, no Demucs for speed), trains LightGBM,
saves the artifact to `backend/models/lane_v1/`. Resumable: cancel
mid-way and re-running picks up from the manifest + shard files.

To restart the backend with the new model:

```powershell
$env:USE_LEARNED_LANES = "1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Per-stage CLIs (if you want manual control)

```bash
# Pull a chart corpus (no auth needed, uses nerinyan.moe mirror)
python -m app.training.collect --target 1000 --no-auth

# Extract features. Drop --use-demucs for a 10x speedup at lower quality.
python -m app.training.train_lane extract --use-beat-this --use-mert

# Train
python -m app.training.train_lane train --num-rounds 500
```

## OAuth alternative (optional)

If you'd rather use the official osu! API for canonical search results,
register an OAuth app at https://osu.ppy.sh/home/account/edit#oauth and
set `OSU_CLIENT_ID` + `OSU_CLIENT_SECRET` env vars. Then drop the
`--no-auth` flag. The mirror path works for everyone; OAuth is purely
nice-to-have.

## Layout

```
app/training/
  osu_api.py        OAuth2 client_credentials + rate-limited search/fetch
  osu_parse.py      .osu chart file parser (pure Python, no deps)
  collect.py        End-to-end corpus collector (CLI)
  osz_extract.py    Pull audio out of .osz archives
  features.py       Per-event feature extraction (reuses pipeline)
  train_lane.py     LightGBM trainer + CLI (extract / train subcommands)
```

## Secrets

Secrets stay in env vars only. The collector reads `OSU_CLIENT_ID` and
`OSU_CLIENT_SECRET` at runtime; they are never written to disk by this
package. Do NOT commit a `.env` file with real credentials.

## License posture

`cache/training/` is `.gitignore`d. The training corpus is collected
for research/non-commercial use and is never redistributed by us. The
trained model artifact ships; the raw .osu / audio data does not. See
`docs/PHASE5_PLAN.md` (License + ethics section).

## When the integration is "ready"

A trained model at `backend/models/lane_v1/` plus `USE_LEARNED_LANES=1`
in the serving env. The pipeline picks lanes via the model; any failure
(missing model, schema mismatch, lightgbm not installed, inference error)
silently falls back to the rule-based assigner. The `/healthz` endpoint
exposes `learnedLanesFlag` and `learnedLanesActive` so the overlay can
render an indicator.
