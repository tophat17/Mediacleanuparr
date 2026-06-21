"""Thin async clients for the Radarr and Sonarr v3 REST APIs."""
from __future__ import annotations

from typing import Any, Optional

import httpx


class ArrError(Exception):
    pass


class ArrClient:
    """Base client for *arr apps. Radarr and Sonarr share the same v3 shape."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v3/{path.lstrip('/')}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.base_url:
            raise ArrError("No URL configured")
        if not self.api_key:
            raise ArrError("No API key configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.request(
                    method, self._url(path), headers=self._headers, **kwargs
                )
            except httpx.RequestError as exc:
                raise ArrError(f"Connection failed: {exc}") from exc
        if resp.status_code == 401:
            raise ArrError("Unauthorized - check the API key")
        if resp.status_code >= 400:
            raise ArrError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    async def test_connection(self) -> dict[str, Any]:
        resp = await self._request("GET", "system/status")
        data = resp.json()
        return {
            "ok": True,
            "version": data.get("version"),
            "app": data.get("appName") or data.get("instanceName"),
        }

    async def get_tags(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "tag")
        return resp.json()

    async def ensure_tag(self, label: str) -> int:
        """Return the id of a tag with this label, creating it if needed."""
        tags = await self.get_tags()
        for t in tags:
            if t.get("label", "").lower() == label.lower():
                return int(t["id"])
        resp = await self._request("POST", "tag", json={"label": label})
        return int(resp.json()["id"])


class RadarrClient(ArrClient):
    async def get_movies(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "movie")
        return resp.json()

    async def delete_movie(
        self, movie_id: int, delete_files: bool, add_exclusion: bool
    ) -> None:
        params = {
            "deleteFiles": str(bool(delete_files)).lower(),
            "addImportExclusion": str(bool(add_exclusion)).lower(),
        }
        await self._request("DELETE", f"movie/{movie_id}", params=params)

    # --- re-add / unblock support -------------------------------------
    async def get_quality_profiles(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "qualityprofile")).json()

    async def get_root_folders(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "rootfolder")).json()

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[dict[str, Any]]:
        data = (await self._request("GET", "movie", params={"tmdbId": int(tmdb_id)})).json()
        return data[0] if data else None

    async def lookup_movie(self, tmdb_id: int) -> Optional[dict[str, Any]]:
        data = (await self._request("GET", "movie/lookup",
                                    params={"term": f"tmdb:{int(tmdb_id)}"})).json()
        return data[0] if data else None

    async def list_exclusions(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "exclusions")).json()

    async def delete_exclusion(self, exclusion_id: int) -> None:
        await self._request("DELETE", f"exclusions/{int(exclusion_id)}")

    async def remove_exclusion_for_tmdb(self, tmdb_id: int) -> bool:
        """Delete any Radarr import-list exclusion matching this TMDb id."""
        removed = False
        for ex in await self.list_exclusions():
            if int(ex.get("tmdbId") or 0) == int(tmdb_id):
                await self.delete_exclusion(int(ex["id"]))
                removed = True
        return removed

    async def set_movie_monitored(self, movie_id: int, monitored: bool = True) -> None:
        movie = (await self._request("GET", f"movie/{movie_id}")).json()
        movie["monitored"] = monitored
        await self._request("PUT", f"movie/{movie_id}", json=movie)

    async def add_movie(self, lookup: dict[str, Any], quality_profile_id: int,
                        root_folder_path: str, search: bool = True) -> dict[str, Any]:
        payload = dict(lookup)
        payload.update({
            "qualityProfileId": int(quality_profile_id),
            "rootFolderPath": root_folder_path,
            "monitored": True,
            "minimumAvailability": payload.get("minimumAvailability") or "released",
            "addOptions": {"searchForMovie": bool(search)},
        })
        return (await self._request("POST", "movie", json=payload)).json()

    async def search_movie(self, movie_id: int) -> None:
        await self._request("POST", "command",
                            json={"name": "MoviesSearch", "movieIds": [int(movie_id)]})


class SonarrClient(ArrClient):
    async def get_series(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "series")
        return resp.json()

    async def delete_series(
        self, series_id: int, delete_files: bool, add_exclusion: bool
    ) -> None:
        params = {
            "deleteFiles": str(bool(delete_files)).lower(),
            "addImportListExclusion": str(bool(add_exclusion)).lower(),
        }
        await self._request("DELETE", f"series/{series_id}", params=params)

    async def unmonitor_series(self, series_id: int) -> None:
        # Fetch, flip monitored false, PUT back.
        resp = await self._request("GET", f"series/{series_id}")
        series = resp.json()
        series["monitored"] = False
        # Also unmonitor all seasons.
        for season in series.get("seasons", []):
            season["monitored"] = False
        await self._request("PUT", f"series/{series_id}", json=series)

    async def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"episodefile?seriesId={series_id}")
        return resp.json()

    async def delete_episode_files(self, series_id: int) -> int:
        """Delete every episode file for a series; keep the series record.

        Returns the number of files deleted. Uses the bulk endpoint when there
        is anything to remove.
        """
        files = await self.get_episode_files(series_id)
        ids = [int(f["id"]) for f in files if f.get("id") is not None]
        if not ids:
            return 0
        await self._request("DELETE", "episodefile/bulk", json={"episodeFileIds": ids})
        return len(ids)

    async def tag_series(self, series_id: int, tag_id: int) -> None:
        resp = await self._request("GET", f"series/{series_id}")
        series = resp.json()
        tags = set(series.get("tags", []))
        tags.add(tag_id)
        series["tags"] = sorted(tags)
        await self._request("PUT", f"series/{series_id}", json=series)

    # --- re-add / unblock support -------------------------------------
    async def get_quality_profiles(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "qualityprofile")).json()

    async def get_language_profiles(self) -> list[dict[str, Any]]:
        try:
            return (await self._request("GET", "languageprofile")).json()
        except ArrError:
            return []  # Sonarr v4 dropped language profiles

    async def get_root_folders(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "rootfolder")).json()

    async def get_series_by_tvdb(self, tvdb_id: int) -> Optional[dict[str, Any]]:
        data = (await self._request("GET", "series", params={"tvdbId": int(tvdb_id)})).json()
        return data[0] if data else None

    async def lookup_series(self, tvdb_id: int) -> Optional[dict[str, Any]]:
        data = (await self._request("GET", "series/lookup",
                                    params={"term": f"tvdb:{int(tvdb_id)}"})).json()
        return data[0] if data else None

    async def list_exclusions(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "importlistexclusion")).json()

    async def delete_exclusion(self, exclusion_id: int) -> None:
        await self._request("DELETE", f"importlistexclusion/{int(exclusion_id)}")

    async def remove_exclusion_for_tvdb(self, tvdb_id: int) -> bool:
        removed = False
        for ex in await self.list_exclusions():
            if int(ex.get("tvdbId") or 0) == int(tvdb_id):
                await self.delete_exclusion(int(ex["id"]))
                removed = True
        return removed

    async def monitor_series(self, series_id: int) -> None:
        series = (await self._request("GET", f"series/{series_id}")).json()
        series["monitored"] = True
        for season in series.get("seasons", []):
            season["monitored"] = True
        await self._request("PUT", f"series/{series_id}", json=series)

    async def add_series(self, lookup: dict[str, Any], quality_profile_id: int,
                         root_folder_path: str, language_profile_id: Optional[int] = None,
                         search: bool = True) -> dict[str, Any]:
        payload = dict(lookup)
        for season in payload.get("seasons", []):
            season["monitored"] = True
        payload.update({
            "qualityProfileId": int(quality_profile_id),
            "rootFolderPath": root_folder_path,
            "monitored": True,
            "seasonFolder": payload.get("seasonFolder", True),
            "addOptions": {"searchForMissingEpisodes": bool(search), "monitor": "all"},
        })
        if language_profile_id is not None:
            payload["languageProfileId"] = int(language_profile_id)
        return (await self._request("POST", "series", json=payload)).json()

    async def search_series(self, series_id: int) -> None:
        await self._request("POST", "command",
                            json={"name": "SeriesSearch", "seriesId": int(series_id)})
