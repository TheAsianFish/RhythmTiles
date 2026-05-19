"""osu! API v2 client (client_credentials grant) + community mirror fetcher.

Two clients in this module:

  - `OsuClient`: official osu! API v2 + mirror download. Requires
    OSU_CLIENT_ID / OSU_CLIENT_SECRET in env, which means the user
    must register an OAuth app on osu! first. Use when canonical
    search results matter (e.g. ranked-status filtering against the
    source of truth).

  - `NerinyanClient`: no-auth, mirror-only path that uses nerinyan.moe
    for BOTH search and .osz download. Sufficient for the training
    corpus and the path Claude Code can run end-to-end without the
    user having to register anything.

Both share rate-limiting and follow the same throttling discipline
(60/min on search, 1/sec on bulk download).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("beatbridge.training.osu_api")

# Endpoints.
_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
_API_BASE = "https://osu.ppy.sh/api/v2"
_OSU_CHART_URL = "https://osu.ppy.sh/osu/{beatmap_id}"

# Default mirror. nerinyan was the most actively maintained mirror as of
# May 2026. Override with OSU_MIRROR_BASE=... if the user prefers another.
_DEFAULT_MIRROR = "https://api.nerinyan.moe"
_MIRROR_OSZ_PATH = "/d/{beatmapset_id}"

# Throttle. 60 req/min sustained on the API (etiquette cap below the
# official 1200 hard cap); 1 req/sec sustained on mirror downloads.
_MIN_API_INTERVAL_S = 1.0
_MIN_MIRROR_INTERVAL_S = 1.0

# Reasonable timeouts. Mirror download timeout is large because some
# .osz files exceed 50MB.
_API_TIMEOUT_S = 30.0
_MIRROR_TIMEOUT_S = 120.0


class OsuApiError(RuntimeError):
    """Network or API failure that the caller should surface to the user."""


@dataclass
class BeatmapSearchHit:
    """One result from /beatmapsets/search filtered to 4K mania."""

    beatmap_id: int
    beatmapset_id: int
    title: str
    artist: str
    creator: str
    version: str  # difficulty name, e.g. "Insane"
    star_rating: float
    bpm: float
    total_length_s: int
    status: str  # "ranked", "approved"


class OsuClient:
    """Thin synchronous wrapper around the osu! v2 endpoints we use.

    Token is acquired lazily on first request and refreshed when it
    expires. The httpx client is kept open between calls for keep-alive.
    """

    def __init__(
        self,
        *,
        client_id: int | None = None,
        client_secret: str | None = None,
        mirror_base: str | None = None,
    ) -> None:
        self._client_id = client_id or _require_int_env("OSU_CLIENT_ID")
        self._client_secret = client_secret or _require_env("OSU_CLIENT_SECRET")
        self._mirror_base = mirror_base or os.environ.get(
            "OSU_MIRROR_BASE", _DEFAULT_MIRROR,
        )
        self._http = httpx.Client(timeout=_API_TIMEOUT_S, follow_redirects=True)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_api_call: float = 0.0
        self._last_mirror_call: float = 0.0

    # -- public API --------------------------------------------------------

    def search_ranked_4k(
        self,
        *,
        cursor: dict[str, Any] | None = None,
    ) -> tuple[list[BeatmapSearchHit], dict[str, Any] | None]:
        """One page of ranked + approved 4K mania difficulties.

        Returns (hits, next_cursor). When next_cursor is None there are
        no more pages. Pass next_cursor verbatim on the next call to
        continue. Default page size is 50 per osu!.
        """
        params: dict[str, Any] = {
            "m": 3,                # mania
            "s": "ranked",         # ranked status (use _search_status_loop for "approved" too)
            "q": "keys=4",         # mania key-count filter
            "sort": "ranked_desc",
        }
        if cursor:
            params["cursor[approved_date]"] = cursor.get("approved_date")
            params["cursor[_id]"] = cursor.get("_id")

        data = self._api_get("/beatmapsets/search", params=params)
        hits = list(_extract_4k_hits(data))
        next_cursor = data.get("cursor")
        return hits, next_cursor

    def fetch_chart(self, beatmap_id: int) -> bytes:
        """Download the .osu chart file. Unauth; goes direct to osu.ppy.sh."""
        self._throttle_api()
        url = _OSU_CHART_URL.format(beatmap_id=beatmap_id)
        resp = self._http.get(url)
        if resp.status_code != 200:
            raise OsuApiError(
                f"fetch_chart({beatmap_id}): HTTP {resp.status_code}",
            )
        return resp.content

    def fetch_osz(self, beatmapset_id: int, *, dest: Path) -> Path:
        """Download a full .osz archive from the community mirror.

        Streams to disk to avoid pulling 50MB+ archives into memory.
        Returns the dest path on success; raises OsuApiError otherwise.
        """
        self._throttle_mirror()
        url = self._mirror_base.rstrip("/") + _MIRROR_OSZ_PATH.format(
            beatmapset_id=beatmapset_id,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http.stream(
                "GET", url, timeout=_MIRROR_TIMEOUT_S,
            ) as resp:
                if resp.status_code != 200:
                    raise OsuApiError(
                        f"fetch_osz({beatmapset_id}): HTTP {resp.status_code}",
                    )
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
        except httpx.HTTPError as exc:
            raise OsuApiError(f"fetch_osz({beatmapset_id}): {exc}") from exc
        return dest

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OsuClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _api_get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        """Authenticated GET against /api/v2. Refreshes token if expired."""
        self._throttle_api()
        token = self._get_token()
        url = _API_BASE + path
        try:
            resp = self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise OsuApiError(f"GET {path}: {exc}") from exc
        if resp.status_code == 401:
            # Token rejected; force refresh and retry once.
            self._token = None
            self._token_expires_at = 0.0
            token = self._get_token()
            self._throttle_api()
            resp = self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 429:
            raise OsuApiError("rate limited by osu! API (HTTP 429)")
        if resp.status_code != 200:
            raise OsuApiError(
                f"GET {path}: HTTP {resp.status_code} body={resp.text[:200]}",
            )
        return resp.json()

    def _get_token(self) -> str:
        """Return a valid bearer token, fetching a new one if expired."""
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        try:
            resp = self._http.post(
                _TOKEN_URL,
                json={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
            )
        except httpx.HTTPError as exc:
            raise OsuApiError(f"oauth token: {exc}") from exc
        if resp.status_code != 200:
            raise OsuApiError(
                f"oauth token: HTTP {resp.status_code} body={resp.text[:200]}",
            )
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = now + int(data.get("expires_in", 3600))
        return self._token  # type: ignore[return-value]

    def _throttle_api(self) -> None:
        wait = _MIN_API_INTERVAL_S - (time.time() - self._last_api_call)
        if wait > 0:
            time.sleep(wait)
        self._last_api_call = time.time()

    def _throttle_mirror(self) -> None:
        wait = _MIN_MIRROR_INTERVAL_S - (time.time() - self._last_mirror_call)
        if wait > 0:
            time.sleep(wait)
        self._last_mirror_call = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_4k_hits(
    search_response: dict[str, Any],
) -> list[BeatmapSearchHit]:
    """Flatten /beatmapsets/search response into per-difficulty hits.

    Each beatmapset contains a `beatmaps` array; one entry per difficulty.
    We keep only mania mode (mode_int==3) with CS==4 (4K).
    """
    out: list[BeatmapSearchHit] = []
    for bset in search_response.get("beatmapsets", []):
        beatmapset_id = int(bset.get("id"))
        title = bset.get("title", "")
        artist = bset.get("artist", "")
        creator = bset.get("creator", "")
        for bmap in bset.get("beatmaps", []):
            mode = int(bmap.get("mode_int", -1))
            if mode != 3:
                continue
            cs = float(bmap.get("cs", -1))
            if int(cs) != 4:
                continue
            out.append(
                BeatmapSearchHit(
                    beatmap_id=int(bmap.get("id")),
                    beatmapset_id=beatmapset_id,
                    title=title,
                    artist=artist,
                    creator=creator,
                    version=bmap.get("version", ""),
                    star_rating=float(bmap.get("difficulty_rating", 0.0)),
                    bpm=float(bmap.get("bpm") or 0.0),
                    total_length_s=int(bmap.get("total_length", 0)),
                    status=str(bmap.get("status", "")),
                ),
            )
    return out


class NerinyanClient:
    """No-auth mirror-only client. Same surface as OsuClient minus OAuth.

    Uses nerinyan.moe for both search and .osz download. Best when you
    don't want to register an OAuth app, or when running in an
    environment (CI, Claude Code) where you can't.

    Limitations vs OsuClient:
      - Search result freshness depends on the mirror's sync cadence.
      - No per-difficulty `/api/v2/beatmaps/{id}` lookups (mirror gives
        the whole beatmapset; we filter to 4K mania in Python).
    """

    def __init__(self, *, mirror_base: str | None = None) -> None:
        self._mirror_base = mirror_base or os.environ.get(
            "OSU_MIRROR_BASE", _DEFAULT_MIRROR,
        )
        self._http = httpx.Client(
            timeout=_API_TIMEOUT_S,
            follow_redirects=True,
            # nerinyan rejects requests without a UA in some cases.
            headers={"User-Agent": "BeatBridge-Training/0.1 (research)"},
        )
        self._last_search_call: float = 0.0
        self._last_download_call: float = 0.0

    def search_ranked_4k(
        self,
        *,
        page: int = 1,
    ) -> list[BeatmapSearchHit]:
        """One page of ranked 4K mania difficulties from the mirror.

        Returns up to 50 hits per page. Caller increments `page` to walk
        more results.

        nerinyan exposes `/search?m=3&q=keys%3D4&s=ranked&p=N`. The
        response is a flat list of beatmapset dicts; we flatten each
        into per-difficulty hits and filter to mode==3 + cs==4.
        """
        self._throttle_search()
        params = {
            "m": 3,                       # mania
            "q": "keys=4",
            "s": "ranked",
            "p": page,
        }
        url = self._mirror_base.rstrip("/") + "/search"
        try:
            resp = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OsuApiError(f"mirror search: {exc}") from exc
        if resp.status_code != 200:
            raise OsuApiError(
                f"mirror search: HTTP {resp.status_code} body={resp.text[:200]}",
            )
        data = resp.json()
        # nerinyan returns a top-level list; the official API returns
        # {"beatmapsets": [...]}. Normalise.
        if isinstance(data, list):
            beatmapsets = data
        else:
            beatmapsets = data.get("beatmapsets", [])
        return list(_flatten_mirror_search({"beatmapsets": beatmapsets}))

    def fetch_osz(self, beatmapset_id: int, *, dest: Path) -> Path:
        """Stream a .osz to disk from the mirror. Same surface as OsuClient."""
        self._throttle_download()
        url = self._mirror_base.rstrip("/") + _MIRROR_OSZ_PATH.format(
            beatmapset_id=beatmapset_id,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http.stream("GET", url, timeout=_MIRROR_TIMEOUT_S) as resp:
                if resp.status_code != 200:
                    raise OsuApiError(
                        f"fetch_osz({beatmapset_id}): HTTP {resp.status_code}",
                    )
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
        except httpx.HTTPError as exc:
            raise OsuApiError(f"fetch_osz({beatmapset_id}): {exc}") from exc
        return dest

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NerinyanClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle_search(self) -> None:
        wait = _MIN_API_INTERVAL_S - (time.time() - self._last_search_call)
        if wait > 0:
            time.sleep(wait)
        self._last_search_call = time.time()

    def _throttle_download(self) -> None:
        wait = _MIN_MIRROR_INTERVAL_S - (time.time() - self._last_download_call)
        if wait > 0:
            time.sleep(wait)
        self._last_download_call = time.time()


def _flatten_mirror_search(payload: dict[str, Any]) -> list[BeatmapSearchHit]:
    """Flatten a mirror search response into 4K mania per-difficulty hits."""
    out: list[BeatmapSearchHit] = []
    for bset in payload.get("beatmapsets", []):
        try:
            beatmapset_id = int(bset.get("id"))
        except (TypeError, ValueError):
            continue
        title = bset.get("title", "")
        artist = bset.get("artist", "")
        creator = bset.get("creator", "")
        for bmap in bset.get("beatmaps", []):
            mode_int = bmap.get("mode_int")
            mode_str = bmap.get("mode", "")
            if mode_int is None and mode_str:
                mode_int = 3 if mode_str == "mania" else -1
            if int(mode_int or -1) != 3:
                continue
            # CRITICAL: skip auto-converts (osu!standard maps the client
            # plays as mania at runtime). These appear in mania search
            # results but the .osz only contains the original standard
            # chart, so chart extraction always fails. The mirror exposes
            # this as a "convert" bool; the official API does too.
            if bool(bmap.get("convert", False)):
                continue
            cs = bmap.get("cs", -1)
            try:
                if int(float(cs)) != 4:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                beatmap_id = int(bmap.get("id"))
            except (TypeError, ValueError):
                continue
            out.append(
                BeatmapSearchHit(
                    beatmap_id=beatmap_id,
                    beatmapset_id=beatmapset_id,
                    title=title,
                    artist=artist,
                    creator=creator,
                    version=bmap.get("version", ""),
                    star_rating=float(bmap.get("difficulty_rating", 0.0) or 0.0),
                    bpm=float(bmap.get("bpm") or 0.0),
                    total_length_s=int(bmap.get("total_length", 0) or 0),
                    status=str(bmap.get("status", bset.get("status", "ranked"))),
                ),
            )
    return out


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise OsuApiError(
            f"environment variable {name} is required. Set it before running training.",
        )
    return value


def _require_int_env(name: str) -> int:
    value = _require_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise OsuApiError(f"{name} must be an integer") from exc
