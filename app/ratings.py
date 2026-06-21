"""User-rating resolution (audience scores) via Radarr/Sonarr + TheMovieDB.

Order of resolution:
  1. Radarr/Sonarr `ratings` blob (TMDb, then IMDb) - free, no API calls. Since
     TMDb is Radarr's primary metadata source, this already covers almost every
     title.
  2. Cached TMDb API result.
  3. TheMovieDB API (vote_average, 0-10 -> 0-100) for the few titles the arr
     apps can't rate. TMDb has no daily request cap, so this scales to large
     libraries (unlike OMDb's 1000/day limit).

Scores are the audience vote average, not critics. Titles with no rating (or no
votes) resolve to None -> needs-review, and are never proposed for deletion.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from . import config, database

TMDB_ENDPOINT = os.environ.get("TMDB_API_BASE", "https://api.themoviedb.org/3").rstrip("/")


def _to_100(raw: Any) -> Optional[int]:
    """Convert a 0-10 rating to a 0-100 integer; None if unparseable/out of range."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    score = int(round(v * 10))
    return score if 0 <= score <= 100 else None


def _arr_user(arr_ratings: Any) -> Optional[int]:
    """Extract a user rating (0-100) from an arr `ratings` object.

    Prefers TMDb, then IMDb (both audience scores). Critics sources
    (rottenTomatoes, metacritic) are ignored.
    """
    if not isinstance(arr_ratings, dict):
        return None
    for key in ("tmdb", "imdb"):
        node = arr_ratings.get(key)
        val = node.get("value") if isinstance(node, dict) else node
        s = _to_100(val)
        if s is not None:
            return s
    return None


def _fresh_engine(source: Optional[str]) -> bool:
    """Trust a cache row only if a previous run of this engine wrote it."""
    if not source:
        return False
    return source.startswith("TMDb") or source.startswith("Radarr/Sonarr")


def _vote_from(obj: Any) -> Optional[int]:
    """Audience score (0-100) from a TMDb object; None if there are no votes."""
    if not isinstance(obj, dict):
        return None
    if int(obj.get("vote_count") or 0) <= 0:
        return None
    return _to_100(obj.get("vote_average"))


async def _tmdb_get(path: str, api_key: str, params: Optional[dict] = None) -> Any:
    """GET a TMDb endpoint. Returns parsed JSON, or None on failure."""
    p = {"api_key": api_key}
    if params:
        p.update(params)
    url = f"{TMDB_ENDPOINT}/{path.lstrip('/')}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=p)
        except httpx.RequestError:
            return None
        if resp.status_code == 429 and attempt == 0:
            # TMDb has no daily cap, but does throttle per-second; brief retry.
            try:
                await asyncio.sleep(float(resp.headers.get("Retry-After", "1")) or 1.0)
            except (ValueError, TypeError):
                await asyncio.sleep(1.0)
            continue
        if resp.status_code != 200:
            try:
                return resp.json()  # surfaces {"success": false, "status_message": ...}
            except ValueError:
                return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


async def _fetch_tmdb(media_type: str, tmdb_id: Any, imdb_id: Optional[str],
                      tvdb_id: Any, api_key: str) -> Optional[int]:
    """Resolve an audience score (0-100) from TMDb. None if unavailable."""
    # Direct lookup by TMDb id (Radarr/Sonarr usually provide it).
    if tmdb_id:
        path = f"movie/{tmdb_id}" if media_type == "movie" else f"tv/{tmdb_id}"
        data = await _tmdb_get(path, api_key)
        v = _vote_from(data)
        if v is not None:
            return v
    # Fall back to the /find endpoint using an external id.
    ext: Optional[str] = None
    src: Optional[str] = None
    if imdb_id:
        ext, src = imdb_id, "imdb_id"
    elif tvdb_id:
        ext, src = str(tvdb_id), "tvdb_id"
    if ext and src:
        data = await _tmdb_get(f"find/{ext}", api_key, {"external_source": src})
        if isinstance(data, dict):
            bucket = "movie_results" if media_type == "movie" else "tv_results"
            results = data.get(bucket) or data.get("movie_results") or data.get("tv_results") or []
            if results:
                return _vote_from(results[0])
    return None


async def test_tmdb(api_key: str) -> dict[str, Any]:
    """Verify a TMDb API key with a known lookup (The Shawshank Redemption)."""
    if not api_key:
        return {"ok": False, "error": "no TMDb API key set"}
    data = await _tmdb_get("movie/278", api_key)
    if data is None:
        return {"ok": False, "error": "TMDb request failed - check the key / network"}
    if isinstance(data, dict) and data.get("success") is False:
        return {"ok": False, "error": data.get("status_message") or "invalid TMDb API key"}
    title = data.get("title") if isinstance(data, dict) else None
    v = _to_100(data.get("vote_average")) if isinstance(data, dict) else None
    detail = title or "sample lookup OK"
    if v is not None:
        detail = f"{detail} - user rating {v}/100"
    return {"ok": True, "app": "TheMovieDB", "detail": detail}


async def resolve_user_score(
    media_type: str,
    tmdb_id: Any = None,
    imdb_id: Optional[str] = None,
    tvdb_id: Any = None,
    arr_ratings: Any = None,
) -> tuple[Optional[int], str]:
    """Return (user_rating_0_100_or_None, source_label). Radarr/Sonarr first."""
    # 1. Radarr/Sonarr provided rating - primary, free, always fresh.
    arr_score = _arr_user(arr_ratings)
    if arr_score is not None:
        return arr_score, "Radarr/Sonarr"

    api_key = str(config.get("tmdb_api_key") or "")
    ttl = config.RATING_CACHE_TTL_SECONDS
    cache_key = imdb_id or (f"tmdb:{tmdb_id}" if tmdb_id else (f"tvdb:{tvdb_id}" if tvdb_id else None))

    # 2. Cached TMDb result (saves repeat calls across scans).
    if cache_key:
        cached = database.get_cached_rating(cache_key, ttl)
        if cached is not None and _fresh_engine(cached.get("source")):
            if cached["rt_score"] is not None:
                return int(cached["rt_score"]), cached["source"] or "cache"
            return None, cached["source"] or "cache (no rating)"

    # 3. TMDb API fallback.
    if api_key and (tmdb_id or imdb_id or tvdb_id):
        v = await _fetch_tmdb(media_type, tmdb_id, imdb_id, tvdb_id, api_key)
        if cache_key:
            database.put_cached_rating(
                cache_key, v, "TMDb" if v is not None else "TMDb (no rating)", {"v": v}
            )
        if v is not None:
            return v, "TMDb"

    return None, "unavailable"
