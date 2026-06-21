"""Auto-unblock logic.

When a human re-requests a previously-removed title in Overseerr/Jellyseerr, we
lift the re-download block: remove the Radarr/Sonarr exclusion, re-monitor the
title, and re-add + search it if it's no longer in the library.

Two paths:
  * Recorded block — a block this app saved during a deletion (also covers the
    Sonarr "unmonitor" case, which has no exclusion).
  * Fallback — no recorded block, but the requested title has a matching
    exclusion in Radarr/Sonarr (e.g. from before this feature existed). On an
    explicit re-request we clear that exclusion too and re-add & search.
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


async def _unblock_movie(tmdb_id: Optional[int], require_exclusion: bool = False) -> Optional[str]:
    """Lift a movie block. With require_exclusion=True, do nothing unless a
    matching Radarr exclusion actually existed (returns None)."""
    if tmdb_id is None:
        return None if require_exclusion else "no tmdb id — cannot unblock movie"
    rc = _radarr()
    notes: list[str] = []
    removed = await rc.remove_exclusion_for_tmdb(tmdb_id)
    if removed:
        notes.append("removed Radarr exclusion")
    elif require_exclusion:
        return None  # nothing was blocking it; leave the request to Seerr
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


async def _unblock_series(tvdb_id: Optional[int], remove_exclusion: bool,
                          require_exclusion: bool = False) -> Optional[str]:
    if tvdb_id is None:
        return None if require_exclusion else "no tvdb id — cannot unblock series"
    sc = _sonarr()
    notes: list[str] = []
    removed = await sc.remove_exclusion_for_tvdb(tvdb_id) if remove_exclusion else False
    if removed:
        notes.append("removed Sonarr exclusion")
    elif require_exclusion:
        return None
    existing = await sc.get_series_by_tvdb(tvdb_id)
    if existing:
        await sc.monitor_series(int(existing["id"]))
        await sc.search_series(int(existing["id"]))
        notes.append("re-monitored & searched")
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
    """Lift any block on a re-requested title — a block this app recorded, or
    any matching Radarr/Sonarr exclusion on an explicit re-request."""
    blocks = database.active_blocks_for(media_type or None, tmdb_id, tvdb_id)

    if blocks:
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
                                    f"unblock:{bt}", True, detail or "no action")
                actions.append({"block_id": b["id"], "title": title, "type": bt,
                                "ok": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - a webhook must never 500
                log.exception("unblock failed for %s", title)
                database.log_action(None, b.get("media_type"), None, title,
                                    f"unblock:{bt}", False, str(exc))
                actions.append({"block_id": b["id"], "title": title, "type": bt,
                                "ok": False, "error": str(exc)})
        return {"unblocked": any(a.get("ok") for a in actions), "actions": actions}

    # No recorded block: clear any matching exclusion in Radarr/Sonarr (the
    # user opted in to "any re-request clears a matching exclusion").
    try:
        if media_type == "tv":
            detail = await _unblock_series(tvdb_id, remove_exclusion=True, require_exclusion=True)
        else:
            detail = await _unblock_movie(tmdb_id, require_exclusion=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("fallback unblock failed")
        return {"unblocked": False, "error": str(exc), "actions": []}

    if not detail:
        return {"unblocked": False,
                "reason": "no recorded block and no matching exclusion", "actions": []}
    ident = tvdb_id if media_type == "tv" else tmdb_id
    database.log_action(None, media_type or "?", None, str(ident),
                        "unblock:exclusion", True, detail)
    return {"unblocked": True,
            "actions": [{"type": "exclusion", "ok": True, "detail": detail}]}
