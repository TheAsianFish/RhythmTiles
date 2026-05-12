"""Pydantic models for the Chart JSON contract.

Source of truth is shared/chart-schema.json. This file mirrors that schema.
A test in tests/test_chart_models.py validates a sample chart against both
the JSON Schema and these models so they cannot drift silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PIPELINE_VERSION = "0.1.0"
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
