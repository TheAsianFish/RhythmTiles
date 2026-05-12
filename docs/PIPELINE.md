# How a chart is generated

A note-by-note answer to "are these notes actually synced to the song?". Short
answer: yes, deterministically. Each note's time `t` is the time of a detected
audio event. No randomness, no LLM, no remote API.

## End-to-end algorithm

Input: an audio file (WAV from yt-dlp, or a user upload).

```
audio bytes
   |
   | sf.read + librosa.resample to mono 22050 Hz
   v
mono float32 PCM samples
   |
   | librosa.beat.beat_track  (autocorrelation + DP)
   v
{ bpm, beat times in seconds }
   |
   | librosa.onset.onset_strength  (spectral flux)
   | librosa.onset.onset_detect    (peak picking on the envelope)
   v
[ onset_frames ]
   |
   | librosa.feature.spectral_centroid sampled at each onset frame
   v
[ Onset(t, strength, centroid_hz) ]
   |
   | assign_lanes:
   |   - cutoff = median(centroid_hz)
   |   - low onsets -> lanes {0,1}, high onsets -> lanes {2,3}
   |   - alternation toggle within each band
   |   - anti-cluster: no two notes within 80ms in the same lane
   v
[ RawNote(t, lane, type) ]
   |
   | shape_difficulty:
   |   - target density per beat: easy 1.0, normal 1.5, hard 2.5
   |   - gap-respecting decimation with a lane-variety tiebreaker
   v
[ shaped notes ]
   |
   | wrap in Chart JSON (audio meta + chart meta + notes)
   v
Chart JSON returned to the extension
```

Every `t` in the final chart comes from `librosa.onset.onset_detect`. Every
lane assignment is derived from `librosa.feature.spectral_centroid` at that
frame. The BPM in `chart.audio.bpm` comes from `librosa.beat.beat_track`.

## What "synced" means here

- A note appears at the time an onset was detected in the source audio.
- Onset detection finds energy spikes in the spectrogram: drum hits, vocal
  attacks, synth stabs. These are the things you would tap along to.
- Lane assignment routes lower-pitched onsets (kicks, bass) to the left half
  of the keyboard and higher-pitched onsets (snares, hi-hats, vocals) to the
  right half. Within each half it alternates, so chains feel natural.

The BPM does NOT drive note placement. Notes follow the onset stream, not a
fixed grid. This is intentional: pop music drifts in micro-timing and a
strict-grid chart would feel off when the song breathes.

## How to verify it yourself

Run the sync verification tool:

```powershell
cd backend
.venv\Scripts\Activate.ps1
$env:BACKEND_ALLOW_YTDLP="1"
python -m app.tools.verify_sync --video-id dQw4w9WgXcQ --out .\verify_out
```

That produces:

- `verify_out/mix.wav` — original song at 30% volume with a click at every
  note time. Listen to it. If clicks land on perceived musical events, the
  chart is synced.
- `verify_out/clicks_only.wav` — the click pattern alone.
- `verify_out/beats_only.wav` — the BPM-tracker output as clicks. Should
  feel like a metronome locked to the song.
- A JSON report on stdout with bpm, content hash, first 20 note times,
  lane distribution.

Two charts for the same audio are byte-identical (same content hash). Two
charts for different audio differ in note count, BPM, and onset timings.

## Libraries used and their exact roles

| Library | Function | Role here |
|---|---|---|
| `soundfile` | `sf.read` | WAV decode |
| `librosa` | `librosa.resample` | sample rate normalization to 22050 Hz |
| `librosa` | `librosa.beat.beat_track` | global BPM + beat times (autocorrelation + dynamic programming) |
| `librosa` | `librosa.onset.onset_strength` | spectral flux envelope (per frame) |
| `librosa` | `librosa.onset.onset_detect` | peak picking on the envelope -> onset frames |
| `librosa` | `librosa.feature.spectral_centroid` | per-frame spectral centroid for lane routing |
| `librosa` | `librosa.frames_to_time` | frame index -> seconds |

No ML model is involved. No data is sent off the machine. Demucs is referenced
as an optional extra in `pyproject.toml` but is NOT installed by default and
not on the active code path.

## Where randomness could leak in (and doesn't)

- Beat tracking: deterministic given audio + librosa version.
- Onset detection: deterministic given audio + parameters (we use defaults).
- Lane assignment: deterministic. Order of onsets fully determines lanes.
- Difficulty shaping: deterministic stride.

A given audio file produces the same chart byte-for-byte across runs on the
same machine. The chart cache uses the sha256 of the audio bytes as its key
(first 16 hex chars) to take advantage of this.

## Future: chord notes

The chart contract already permits multiple notes at the same `t`. Today the
lane assigner emits at most one note per onset; chord support would mean:

1. Detect "strong" onsets (top quantile of onset_strength) and split them
   into 2-3 simultaneous notes in different lanes.
2. The lane assigner would emit a small bundle (e.g. lanes 0+3 for the
   strongest, 1+2 for next).
3. No game-loop changes needed: input.ts already scopes by lane, hit
   detection already filters by lane, so two same-t notes in different
   lanes "just work" as a chord.

Add this in `lane_assign.py` when chord requests come in. See the TODO
comment near the strength field on the Onset dataclass.
