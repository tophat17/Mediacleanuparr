"""Auto-unblock logic.

When a human re-requests a previously-removed title in Overseerr/Jellyseerr, we
lift the re-download block this app applied: remove the Radarr/Sonarr exclusion,
re-monitor the title, and re-add + search it if it's no longer in the library.
Only blocks this app recorded (in the `blocks` table) are ever lifted — a title
the user unmonitored or excluded themselves is never touched.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import config, database
from .arr_client import ArrError, RadarrClient, SonarrClient

log = logging.getLogger("mediacleanuparr.unblock")


def _radarr() -> RadarrClient:
    return RadarrClient(str(config.get("radarr_url") or ""),
                        str(config.get("radarr_api_key") or ""))


def _sonarr() -> SonarrClient:
    return SonarrClient(str(config.get("sonarr_url") or ""),
                        str(config.get("sonarr_api_key") or ""))


async def _unblock_movie(tmdb_id: Optional[int]) -> str:
    if tmdb_id is None:
        return "no tmdb id — cannot unblock movie"
    rc = _radarr()
    notes: list[str] = []
    if await rc.remove_exclusion_for_tmdb(tmdb_id):
        notes.append("removed Radarr exclusion")
    existing = await rc.get_movie_by_tmdb(tmdb_id)
    if existing:
        await rc.set_movie_monitored(int(existing["id"]), True)
        await rc.search_movie(int(existing["id"]))
        notes.append("re-monitored & searched")
        return "; ".join(notes)
    lookup = await rc.lookup_movie(tmdb_id)
    qps = await rc.get_quality_profiles()
    rfs = await rc.get_root_folders()
    if not lookup or not qps or not rfs:
        notes.append("exclusion cleared; let Seerr add it (no lookup/profile/root)")
        return "; ".join(notes)
    await rc.add_movie(lookup, int(qps[0]["id"]), rfs[0]["path"], search=True)
    notes.append("re-added & searched")
    return "; ".join(notes)


async def _unblock_series(tvdb_id: Optional[int], remove_exclusion: bool) -> str:
    sc = _sonarr()
    notes: list[str] = []
    if remove_exclusion and tvdb_id is not None and await sc.remove_exclusion_for_tvdb(tvdb_id):
        notes.append("removed Sonarr exclusion")
    existing = await sc.get_series_by_tvdb(tvdb_id) if tvdb_id is not None else None
    if existing:
        await sc.monitor_series(int(existing["id"]))
        await sc.search_series(int(existing["id"]))
        notes.append("re-monitored & searched")
        return "; ".join(notes)
    if tvdb_id is None:
        notes.append("no tvdb id — cannot re-add series")
        return "; ".join(notes)
    lookup = await sc.lookup_series(tvdb_id)
    qps = await sc.get_quality_profiles()
    rfs = await sc.get_root_folders()
    if not lookup or not qps or not rfs:
        notes.append("exclusion cleared; let Seerr add it (no lookup/profile/root)")
        return "; ".join(notes)
    lps = await sc.get_language_profiles()
    lang = int(lps[0]["id"]) if lps else None
    await sc.add_series(lookup, int(qps[0]["id"]), rfs[0]["path"],
                        language_profile_id=lang, search=True)
    notes.append("re-added & searched")
    return "; ".join(notes)


async def unblock_title(media_type: Optional[str], tmdb_id: Optional[int],
                        tvdb_id: Optional[int]) -> dict[str, Any]:
    """Lift any active blocks this app placed on a title. Returns a summary."""
    blocks = database.active_blocks_for(media_type or None, tmdb_id, tvdb_id)
    if not blocks:
        return {"unblocked": False, "reason": "no active block for this title", "actions": []}

    actions: list[dict[str, Any]] = []
    for b in blocks:
        bt = b.get("block_type")
        title = b.get("title") or "?"
        try:
            if bt == "radarr_exclusion":
                detail = await _unblock_movie(b.get("tmdb_id"))
            elif bt == "sonarr_unmonitor":
                detail = await _unblock_series(b.get("tvdb_id"), remove_exclusion=False)
            elif bt == "sonarr_exclusion":
                detail = await _unblock_series(b.get("tvdb_id"), remove_exclusion=True)
            else:
                detail = f"unknown block type {bt}"
            database.deactivate_block(int(b["id"]))
            database.log_action(None, b.get("media_type"), None, title,
                                f"unblock:{bt}", True, detail)
            actions.append({"block_id": b["id"], "title": title, "type": bt,
                            "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - a webhook must never 500
            log.exception("unblock failed for %s", title)
            database.log_action(None, b.get("media_type"), None, title,
                                f"unblock:{bt}", False, str(exc))
            actions.append({"block_id": b["id"], "title": title, "type": bt,
                            "ok": False, "error": str(exc)})
    return {"unblocked": any(a.get("ok") for a in actions), "actions": actions}
