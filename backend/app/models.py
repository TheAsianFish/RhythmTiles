"""Pydantic models for the Chart JSON contract.

Source of truth is shared/chart-schema.json. This file mirrors that schema.
A test in tests/test_chart_models.py validates a sample chart against both
the JSON Schema and these models so they cannot drift silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Pipeline version: bumped any time the chart contents could materially
# differ for the same audio (algorithm changes, ML phase additions, tuning
# of TARGET_NOTES_PER_SEC or chord/hold gates). Bumping invalidates the
# chart SQLite cache via the placeholder-vs-real check + a future
# version-aware invalidation hook.
#
# History:
#   0.1.0  initial v1 pipeline (librosa beats, full-mix onsets,
#          centroid lane routing, heuristic chord emission).
#   0.2.0  Beat This!, Demucs per-stem onsets, MERT section
#          detection, non-overlapping difficulty bands, lane
#          lockup removed. (2026-05-12)
#   0.3.0  per-section thinning: MERT bucket multiplies the global
#          keep ratio so chorus visibly outpaces verse density.
#          Minor-bump (not patch) so the cache flushes; the
#          mode_key invalidation only watches major.minor.
#          (2026-05-13)
#   0.4.0  REVERTS the per-section thinning from 0.3.0. Playtesting
#          showed the per-bucket ranking stripped verses to their
#          strongest notes (= predictable downbeats) and made
#          choruses feel mechanical because total density dropped
#          against the sanity cap. Back to global thinning + the
#          gentle _ENERGY_MULT bias. (2026-05-13)
#   0.5.0  Ranked-osu!mania feel pass:
#           - Hold ratios way up (Easy 4%, Normal 8%, Hard 14%,
#             Expert 20%) so slider/tap balance matches ranked LN
#             charts instead of the prior 1-7%.
#           - Hold detection more permissive: SUSTAIN_THRESHOLD
#             0.75 -> 0.60, MAX_HOLD_BEATS 2 -> 4, MAX_HOLD_S
#             1.5 -> 2.5s.
#           - MERT bucket-2 (chorus) sections route forced half-beat
#             subdivisions at Hard+ and forced quarter-beat
#             subdivisions at Expert. Choruses now play busy.
#           - Crescendo detector more sensitive: 7%->4% rise,
#             3->2 min beats. Pre-chorus builds escalate harder.
#           - _ENERGY_MULT widened: 0.90/1.15 -> 0.80/1.30 so
#             chorus phrasing wins global thinning more reliably.
#          (2026-05-13)
#   0.6.0  Holds reduced from 0.5.0 to ~1.5x original
#          (Easy 1.5%, Normal 3.5%, Hard 6%, Expert 10%); global
#          MAX_HOLD_RATIO 0.18 -> 0.08; hold-detect thresholds
#          and durations dialed between original and 0.5.0.
#          Reverted _ENERGY_MULT and crescendo sensitivity that
#          leaked into ML-light/full. Empty-beat fill triggers on
#          single-beat gaps now (was 2+) so the chart doesn't have
#          perceptible dead spots when one kick goes missing.
#          MERT chorus-subdivision wiring (v0.5.0) kept; affects
#          ML-max only. (2026-05-13)
#   0.7.0  Density bump to match osu!mania ranked rates.
#          TARGET_NOTES_PER_SEC raised across all 4 tiers:
#          Easy 1.1->2.0 mid, Normal 3.15->4.25, Hard 5.25->6.25,
#          Expert 7.05->8.5. Sanity cap 8 -> 12. Paired with
#          overlay panel height 744->900 (max), 504->640 (min)
#          so notes have more runway. Mode-agnostic: bands
#          apply to all 4 ML modes uniformly after their own
#          candidate generation. (2026-05-13)
#   0.8.0  v0.7.0 felt too hard - auto-generated charts at ranked
#          osu!mania density don't have the human pattern intent
#          that makes those charts playable. Pulled bands halfway
#          back: Easy mid 1.5, Normal 3.55, Hard 5.7, Expert 7.7.
#          Sanity cap 12 -> 10. Panel height kept at 0.7.0 values
#          (more runway is always welcome). (2026-05-13)
#   0.9.0  Still too hard. Reverted to original bands times a
#          uniform 1.05 across all 4 tiers. Sanity cap back to 8.
#          The runway bump from 0.7.0 remains the only meaningful
#          UX delta from baseline; density barely changes.
#   0.10.0 ML-max bucketing fix. Energy buckets always come from
#          the per-note RMS curve, not from MERT section means.
#          Per-section quantiles across 3-6 values pushed long
#          verses into bucket 0, which the _ENERGY_MULT 0.90
#          discount then stripped during top-K thinning - the
#          "blank space in ML-max" symptom. MERT still drives
#          chorus-beat subdivision routing and the wire-format
#          sections array. Minor bump to invalidate ML-max chart
#          cache rows that were thinned with the old logic.
#   0.11.0 Time-windowed difficulty thinner. shape_difficulty no
#          longer does global top-K (which clustered surviving notes
#          around strong moments and left quiet sections dead). Now
#          walks the chart in 2s windows and keeps top-K-per-window
#          by score, with a shortfall backfill to keep total NPS in
#          the difficulty's band on sparse songs. Should fix the
#          "verses feel empty, chorus is busy" complaint without
#          changing per-note timing or chord-pair behaviour.
PIPELINE_VERSION = "0.11.0"
SCHEMA_VERSION = "1.0"

Difficulty = Literal["easy", "normal", "hard", "expert"]
NoteType = Literal["tap", "hold"]
AudioSource = Literal["youtube", "upload", "synthetic"]


class AudioMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: AudioSource
    videoId: str | None = None
    contentHash: str | None = None
    duration: float = Field(ge=0)
    bpm: float = Field(ge=0)
    bpmCurve: list[tuple[float, float]] | None = None


class ChartMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generatedAt: datetime
    pipelineVersion: str = PIPELINE_VERSION
    difficulty: Difficulty
    keyMode: Literal[4] = 4
    title: str | None = None
    artist: str | None = None


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0, description="Seconds from audio t=0.")
    lane: int = Field(ge=0, le=3)
    type: NoteType
    duration: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _hold_needs_duration(self) -> "Note":
        if self.type == "hold" and (self.duration is None or self.duration <= 0):
            raise ValueError("hold notes require a positive duration")
        if self.type == "tap" and self.duration is not None:
            # Forbid duration on taps so we keep the JSON tight.
            raise ValueError("tap notes must not carry a duration")
        return self


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    intensity: float | None = Field(default=None, ge=0, le=1)
    label: str | None = None


class Chart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"] = SCHEMA_VERSION
    audio: AudioMeta
    metadata: ChartMeta
    notes: list[Note]
    sections: list[Section] | None = None


class GenerateRequest(BaseModel):
    """JSON body for POST /charts/generate when not uploading audio."""

    model_config = ConfigDict(extra="forbid")

    videoId: str | None = None
    audioUrl: str | None = None
    difficulty: Difficulty = "normal"
    duration: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _has_source(self) -> "GenerateRequest":
        if not self.videoId and not self.audioUrl:
            raise ValueError("must provide videoId or audioUrl")
        return self
