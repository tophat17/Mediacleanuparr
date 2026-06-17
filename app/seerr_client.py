"""Thin async client for the Overseerr / Jellyseerr ("Seerr") v1 REST API.

Used to keep Seerr in sync when mediacleanuparr removes media: we delete the
item's media record in Seerr so it stops tracking/auto-requeuing it. Deleting
the media record leaves the title fully re-requestable in Seerr — it does NOT
blacklist it — so a user can choose to request it again later.

Docs: Overseerr/Jellyseerr expose /api/v1 with an `X-Api-Key` header.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class SeerrError(Exception):
    pass


class SeerrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 12.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.base_url:
            raise SeerrError("No Seerr URL configured")
        if not self.api_key:
            raise SeerrError("No Seerr API key configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.request(method, self._url(path), headers=self._headers, **kwargs)
            except httpx.RequestError as exc:
                raise SeerrError(f"Connection failed: {exc}") from exc
        if resp.status_code == 401:
            raise SeerrError("Unauthorized — check the Seerr API key")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise SeerrError(f"Seerr returned HTTP {resp.status_code}")
        return resp

    async def test_connection(self) -> dict[str, Any]:
        try:
            resp = await self._request("GET", "status")
            data = resp.json()
            return {"ok": True, "app": "Seerr", "version": str(data.get("version", "?"))}
        except SeerrError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError:
            return {"ok": False, "error": "unexpected response from Seerr"}

    async def find_media_id(self, media_type: str, tmdb_id: Any, tvdb_id: Any = None) -> Optional[int]:
        """Resolve Seerr's internal mediaId for a title, or None if Seerr isn't
        tracking it. Movies are looked up by TMDb id; TV by TMDb id when known,
        else by matching TVDB id in Seerr's media list.
        """
        # Direct detail lookup by TMDb id (gives mediaInfo.id when tracked).
        if tmdb_id:
            path = f"movie/{tmdb_id}" if media_type == "movie" else f"tv/{tmdb_id}"
            try:
                resp = await self._request("GET", path)
                if resp.status_code == 200:
                    info = (resp.json() or {}).get("mediaInfo") or {}
                    mid = info.get("id")
                    if mid is not None:
                        return int(mid)
            except (SeerrError, ValueError):
                pass
        # TV fallback: match by TVDB id in the media list.
        if media_type == "tv" and tvdb_id:
            skip = 0
            for _ in range(25):  # cap pagination
                try:
                    resp = await self._request("GET", "media", params={"take": 100, "skip": skip})
                    payload = resp.json()
                except (SeerrError, ValueError):
                    break
                results = payload.get("results") or []
                for m in results:
                    if str(m.get("tvdbId")) == str(tvdb_id):
                        return int(m["id"]) if m.get("id") is not None else None
                page = payload.get("pageInfo") or {}
                if skip + len(results) >= int(page.get("results", 0)) or not results:
                    break
                skip += 100
        return None

    async def delete_media(self, media_id: int) -> None:
        """Delete a media record in Seerr (clears its request/availability).

        The title remains re-requestable afterwards.
        """
        await self._request("DELETE", f"media/{media_id}")

    async def get_request_map(self) -> dict[str, str]:
        """Return a map of media -> requester display name, keyed by
        'movie:<tmdbId>', 'tv:<tmdbId>' and 'tv:tvdb:<tvdbId>'. Best-effort;
        returns {} on any error so a scan never fails because of Seerr.
        """
        out: dict[str, str] = {}
        if not (self.base_url and self.api_key):
            return out
        skip = 0
        for _ in range(50):  # cap pagination
            try:
                resp = await self._request("GET", "request", params={"take": 100, "skip": skip})
                payload = resp.json()
            except (SeerrError, ValueError):
                break
            results = payload.get("results") or []
            for r in results:
                media = r.get("media") or {}
                by = r.get("requestedBy") or {}
                name = (by.get("displayName") or by.get("plexUsername")
                        or by.get("jellyfinUsername") or by.get("username") or by.get("email"))
                if not name:
                    continue
                mtype = media.get("mediaType") or r.get("type") or ""
                tmdb = media.get("tmdbId")
                tvdb = media.get("tvdbId")
                if tmdb is not None:
                    out[f"{mtype}:{tmdb}"] = name
                if tvdb is not None:
                    out[f"tv:tvdb:{tvdb}"] = name
            page = payload.get("pageInfo") or {}
            total = int(page.get("results", 0) or 0)
            if not results or skip + len(results) >= total:
                break
            skip += 100
        return out
