"""Settings loaded from environment. Kept dependency-light on purpose."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    cache_dir: Path
    allow_ytdlp: bool
    use_demucs: bool
    cors_origins: list[str]
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        cache_dir = Path(os.environ.get("CACHE_DIR", "./cache")).resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        origins_raw = os.environ.get("CORS_ORIGINS", "*").strip()
        origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
        return cls(
            host=os.environ.get("BACKEND_HOST", "0.0.0.0"),
            port=int(os.environ.get("BACKEND_PORT", "8000")),
            cache_dir=cache_dir,
            allow_ytdlp=_bool(os.environ.get("BACKEND_ALLOW_YTDLP"), default=False),
            use_demucs=_bool(os.environ.get("USE_DEMUCS"), default=False),
            cors_origins=origins or ["*"],
            log_level=os.environ.get("LOG_LEVEL", "info"),
        )


settings = Settings.from_env()
