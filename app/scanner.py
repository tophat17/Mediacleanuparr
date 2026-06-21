"""Dry-run scanner with live progress.

Pulls movies/series from the arr apps, resolves IMDb user ratings, and
classifies each item. Nothing is ever deleted here — the scanner only proposes.

A module-level progress object is updated as the scan runs so the UI can show a
progress bar. This is a single-user tool, so only one scan runs at a time.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

from . import config, database
from .arr_client import ArrError, RadarrClient, SonarrClient
from .ratings import resolve_user_score
from .safety import check_path
from .seerr_client import SeerrClient

ACTION_DELETE = "delete"
ACTION_KEEP = "keep"
ACTION_REVIEW = "review"

# --------------------------- progress tracking ----------------------------
_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False,
    "phase": "idle",          # idle | fetching | scanning | done | error
    "scan_id": None,
    "scope": None,
    "total_items": 0,
    "processed_items": 0,
    "total_bytes": 0,
    "scanned_bytes": 0,
    "current_title": "",
    "status": None,
    "error": None,
}


def reset_progress(scope: str) -> None:
    with _progress_lock:
        _progress.update({
            "running": True, "phase": "fetching", "scan_id": None, "scope": scope,
            "total_items": 0, "processed_items": 0, "total_bytes": 0,
            "scanned_bytes": 0, "current_title": "", "status": None, "error": None,
        })


def get_progress() -> dict[str, Any]:
    with _progress_lock:
        return dict(_progress)


def is_running() -> bool:
    with _progress_lock:
        return bool(_progress["running"])


def _set(**kw: Any) -> None:
    with _progress_lock:
        _progress.update(kw)


def mark_error(msg: str) -> None:
    with _progress_lock:
        _progress.update({"running": False, "phase": "error", "error": msg})


def _tick(title: str, size: int) -> None:
    with _progress_lock:
        _progress["processed_items"] += 1
        _progress["scanned_bytes"] += int(size or 0)
        _progress["current_title"] = title


def _imdb_id(obj: dict[str, Any]) -> Optional[str]:
    imdb = obj.get("imdbId")
    if imdb and str(imdb).startswith("tt"):
        return str(imdb)
    return None


def _movie_action(score: Optional[int], threshold: int) -> tuple[str, str, bool]:
    """Return (action, reason, prevent_redownload).

    Items with no rating, or a rating of 0, are never deleted — they're
    surfaced as needs-review only (a 0/missing score usually means the data is
    bad, not that the title is bad).
    """
    if score is None:
        return ACTION_REVIEW, "No user rating available — skipped", False
    if score <= 0:
        return ACTION_REVIEW, "User rating is 0 / unavailable — skipped", False
    if score < threshold:
        return ACTION_DELETE, f"User rating {score}/100 is below threshold {threshold}", True
    return ACTION_KEEP, f"User rating {score}/100 meets threshold {threshold}", False


async def _seerr_request_map() -> dict[str, str]:
    url = str(config.get("seerr_url") or "").strip()
    key = str(config.get("seerr_api_key") or "").strip()
    if not (url and key):
        return {}
    try:
        return await SeerrClient(url, key).get_request_map()
    except Exception:  # noqa: BLE001 - never fail a scan over Seerr
        return {}


def _requester(reqmap: dict[str, str], media_type: str, tmdb_id: Any, tvdb_id: Any) -> Optional[str]:
    if not reqmap:
        return None
    if tmdb_id is not None and reqmap.get(f"{media_type}:{tmdb_id}"):
        return reqmap[f"{media_type}:{tmdb_id}"]
    if media_type == "tv" and tvdb_id is not None and reqmap.get(f"tv:tvdb:{tvdb_id}"):
        return reqmap[f"tv:tvdb:{tvdb_id}"]
    return None


def find_empty_dirs(root: str, cap: int = 300) -> list[str]:
    """Return maximal directories under `root` that contain no files anywhere
    beneath them (orphaned empty folders). Never includes `root` itself.
    """
    empty: set[str] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            children_all_empty = all(os.path.join(dirpath, d) in empty for d in dirnames)
            if not filenames and children_all_empty:
                empty.add(dirpath)
    except OSError:
        return []
    maximal = sorted(d for d in empty if os.path.dirname(d) not in empty)
    return maximal[:cap]


async def _scan_movies(scan_id: int, movies: list[dict[str, Any]], threshold: int,
                       reqmap: dict[str, str], empty_mode: bool,
                       include_unrated: bool, excluded: set[str]) -> dict[str, Any]:
    add_excl = bool(config.get("add_import_exclusion"))
    roots = config.media_roots()

    flagged = review = freed = empty_n = skipped_excl = 0
    for m in movies:
        if m.get("tmdbId") is not None and f"movie:tmdb:{m.get('tmdbId')}" in excluded:
            skipped_excl += 1
            continue
        has_file = bool(m.get("hasFile"))
        path = m.get("path") or ""
        size = int(m.get("sizeOnDisk") or 0)
        requested_by = _requester(reqmap, "movie", m.get("tmdbId"), None)
        score, source = await resolve_user_score(
            "movie", tmdb_id=m.get("tmdbId"), imdb_id=_imdb_id(m), arr_ratings=m.get("ratings"))
        action, reason, prevent = _movie_action(score, threshold)
        _tick(m.get("title") or "", size)

        # Empty-cleanup: a 0-byte / no-file entry becomes a removable candidate
        # ONLY when it's also below the rating threshold. A well-rated empty
        # entry (e.g. a monitored title not downloaded yet) is left alone.
        is_empty = not has_file or size <= 0
        if empty_mode and is_empty and action == ACTION_DELETE:
            database.add_scan_item(scan_id, {
                "media_type": "movie", "arr_id": m.get("id"), "tmdb_id": m.get("tmdbId"),
                "title": m.get("title"), "year": m.get("year"), "score": score,
                "rating_source": source, "path": path, "size_bytes": 0,
                "proposed_action": ACTION_DELETE, "prevent_redl": add_excl,
                "reason": f"Empty (0 bytes) and below threshold {threshold} — will be removed from Radarr",
                "requested_by": requested_by, "selected": True,
            })
            empty_n += 1
            continue

        if action == ACTION_KEEP:
            continue
        if action == ACTION_REVIEW:
            if not include_unrated:
                continue  # hidden unless the user opts in
            review += 1

        path_ok, path_reason = check_path(path, roots) if has_file else (False, "no file on disk")
        if action == ACTION_DELETE:
            if not has_file:
                reason = "Below threshold but no file on disk — enable empty cleanup to remove"
            elif not path_ok:
                reason = f"Below threshold but path is unsafe ({path_reason}) — will NOT delete"
            else:
                freed += size
                flagged += 1

        database.add_scan_item(scan_id, {
            "media_type": "movie", "arr_id": m.get("id"),
            "tmdb_id": m.get("tmdbId"), "title": m.get("title"),
            "year": m.get("year"), "score": score, "rating_source": source,
            "path": path, "size_bytes": size, "proposed_action": action,
            "prevent_redl": prevent and add_excl, "reason": reason,
            "requested_by": requested_by,
            "selected": action == ACTION_DELETE and has_file and path_ok,
        })

    return {"movies_total": len(movies), "movies_flagged": flagged,
            "movies_review": review, "movies_freed_bytes": freed, "movies_empty": empty_n,
            "movies_excluded": skipped_excl}


async def _scan_series(scan_id: int, series: list[dict[str, Any]], threshold: int,
                       unmonitor: bool, reqmap: dict[str, str], empty_mode: bool,
                       include_unrated: bool, excluded: set[str]) -> dict[str, Any]:
    roots = config.media_roots()

    flagged = review = freed = empty_n = skipped_excl = 0
    for s in series:
        tmdb, tvdb = s.get("tmdbId"), s.get("tvdbId")
        if (tmdb is not None and f"tv:tmdb:{tmdb}" in excluded) or \
           (tvdb is not None and f"tv:tvdb:{tvdb}" in excluded):
            skipped_excl += 1
            continue
        path = s.get("path") or ""
        stats = s.get("statistics") or {}
        size = int(stats.get("sizeOnDisk") or 0)
        has_file = size > 0 or int(stats.get("episodeFileCount") or 0) > 0
        requested_by = _requester(reqmap, "tv", s.get("tmdbId"), s.get("tvdbId"))
        score, source = await resolve_user_score(
            "tv", tmdb_id=s.get("tmdbId"), imdb_id=_imdb_id(s),
            tvdb_id=s.get("tvdbId"), arr_ratings=s.get("ratings"))
        action, reason, prevent = _movie_action(score, threshold)
        _tick(s.get("title") or "", size)

        is_empty = not has_file or size <= 0
        if empty_mode and is_empty and action == ACTION_DELETE:
            database.add_scan_item(scan_id, {
                "media_type": "tv", "arr_id": s.get("id"), "tmdb_id": s.get("tmdbId"),
                "tvdb_id": s.get("tvdbId"), "title": s.get("title"), "year": s.get("year"),
                "score": score, "rating_source": source, "path": path, "size_bytes": 0,
                "proposed_action": ACTION_DELETE, "prevent_redl": True,
                "reason": f"Empty (0 bytes) and below threshold {threshold} — will be removed from Sonarr",
                "requested_by": requested_by, "selected": True,
            })
            empty_n += 1
            continue

        if action == ACTION_KEEP:
            continue
        if action == ACTION_REVIEW:
            if not include_unrated:
                continue
            review += 1

        path_ok, path_reason = check_path(path, roots) if has_file else (False, "no files on disk")
        if action == ACTION_DELETE:
            note = "will be unmonitored" if unmonitor else "left monitored (toggle off)"
            if not has_file:
                reason = f"Below threshold but no files on disk — enable empty cleanup to remove"
            elif not path_ok:
                reason = (f"Below threshold but path is unsafe ({path_reason}) — "
                          f"will NOT delete; {note}")
            else:
                freed += size
                flagged += 1
                reason = (f"User rating {score}/100 is below threshold {threshold} — "
                          f"files will be deleted, {note}")

        database.add_scan_item(scan_id, {
            "media_type": "tv", "arr_id": s.get("id"),
            "tmdb_id": s.get("tmdbId"), "tvdb_id": s.get("tvdbId"), "title": s.get("title"),
            "year": s.get("year"), "score": score, "rating_source": source,
            "path": path, "size_bytes": size, "proposed_action": action,
            "prevent_redl": prevent and unmonitor, "reason": reason,
            "requested_by": requested_by,
            "selected": action == ACTION_DELETE and has_file and path_ok,
        })

    return {"series_total": len(series), "series_flagged": flagged,
            "series_review": review, "series_freed_bytes": freed, "series_empty": empty_n,
            "series_excluded": skipped_excl}


async def run_dry_scan(scope: str) -> dict[str, Any]:
    """scope: 'movies' | 'tv' | 'both'. Updates progress as it runs."""
    threshold = int(config.get("min_rt_score"))
    unmonitor = bool(config.get("sonarr_unmonitor"))
    empty_mode = bool(config.get("auto_select_empty"))
    include_unrated = bool(config.get("include_unrated"))
    excluded = database.excluded_keys()

    do_movies = scope in ("movies", "both")
    do_tv = scope in ("tv", "both")

    scan_id = database.create_scan(scope, threshold)
    _set(scan_id=scan_id, phase="fetching")
    summary: dict[str, Any] = {"errors": []}
    reqmap = await _seerr_request_map()

    # Fetch lists first so we know the totals before the slow rating lookups.
    movies: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    if do_movies:
        try:
            rc = RadarrClient(str(config.get("radarr_url") or ""),
                              str(config.get("radarr_api_key") or ""))
            movies = await rc.get_movies()
        except ArrError as exc:
            summary["errors"].append(f"Radarr: {exc}")
    if do_tv:
        try:
            sc = SonarrClient(str(config.get("sonarr_url") or ""),
                              str(config.get("sonarr_api_key") or ""))
            series = await sc.get_series()
        except ArrError as exc:
            summary["errors"].append(f"Sonarr: {exc}")

    total_bytes = (
        sum(int(m.get("sizeOnDisk") or 0) for m in movies)
        + sum(int((s.get("statistics") or {}).get("sizeOnDisk") or 0) for s in series)
    )
    _set(phase="scanning", total_items=len(movies) + len(series), total_bytes=total_bytes)

    if do_movies:
        summary.update(await _scan_movies(scan_id, movies, threshold, reqmap, empty_mode,
                                          include_unrated, excluded))
    if do_tv:
        summary.update(await _scan_series(scan_id, series, threshold, unmonitor, reqmap,
                                          empty_mode, include_unrated, excluded))

    # Empty-cleanup also surfaces orphaned empty folders on disk.
    empty_folders = 0
    if empty_mode:
        for root in config.media_roots():
            for d in find_empty_dirs(root):
                ok, _reason = check_path(d, config.media_roots())
                if not ok:
                    continue
                database.add_scan_item(scan_id, {
                    "media_type": "folder", "arr_id": None, "title": os.path.basename(d) or d,
                    "score": None, "rating_source": "empty folder", "path": d, "size_bytes": 0,
                    "proposed_action": ACTION_DELETE, "prevent_redl": False,
                    "reason": "Orphaned empty folder (0 bytes) — will be removed from disk",
                    "selected": True,
                })
                empty_folders += 1
    summary["empty_folders"] = empty_folders

    summary["total_freed_bytes"] = (
        summary.get("movies_freed_bytes", 0) + summary.get("series_freed_bytes", 0)
    )
    summary["scan_id"] = scan_id
    status = "completed" if not summary["errors"] else "completed_with_errors"
    database.finish_scan(scan_id, status, summary)
    _set(running=False, phase="done", status=status)
    return {"scan_id": scan_id, "status": status, "summary": summary}


def _gb(b: int) -> float:
    return round((b or 0) / 1_000_000_000, 1)


async def run_biggest_scan(scope: str, limit: int,
                           empty_cleanup: bool = False) -> dict[str, Any]:
    """List the N largest items by size on disk (no ratings involved).

    scope: 'movies' | 'tv' | 'both'. Returns a scan whose items are sorted
    largest first; nothing is pre-selected — the user picks what to purge.
    Updates the shared progress object so the UI can show a progress bar.

    When `empty_cleanup` is on, 0-byte entries (no files on disk) are surfaced
    as removable candidates — removed from Radarr/Sonarr with the same
    re-download restriction the rest of the app applies — and orphaned empty
    folders under the media roots are listed for removal. These are pre-selected
    since there is nothing on disk to lose.
    """
    roots = config.media_roots()
    add_excl = bool(config.get("add_import_exclusion"))
    do_movies = scope in ("movies", "both")
    do_tv = scope in ("tv", "both")
    scan_id = database.create_scan(f"biggest:{scope}", 0)
    _set(scan_id=scan_id, phase="fetching")
    reqmap = await _seerr_request_map()
    errors: list[str] = []
    collected: list[tuple[int, dict[str, Any]]] = []

    def _mark_empty(rec: dict[str, Any]) -> None:
        """Turn a no-file arr entry into a removable empty candidate."""
        app_name = "Radarr" if rec["media_type"] == "movie" else "Sonarr"
        block = " & blocked from re-download" if add_excl else ""
        rec.pop("has_file", None)
        rec.update({
            "score": None, "rating_source": "empty", "size_bytes": 0,
            "proposed_action": ACTION_DELETE, "prevent_redl": add_excl,
            "reason": f"Empty (0 bytes / no files) — will be removed from {app_name}{block}",
            "requested_by": _requester(reqmap, rec["media_type"],
                                       rec.get("tmdb_id"), rec.get("tvdb_id")),
            "selected": True,
        })

    if do_movies:
        try:
            rc = RadarrClient(str(config.get("radarr_url") or ""),
                              str(config.get("radarr_api_key") or ""))
            for m in await rc.get_movies():
                size = int(m.get("sizeOnDisk") or 0)
                collected.append((size, {
                    "media_type": "movie", "arr_id": m.get("id"), "tmdb_id": m.get("tmdbId"),
                    "title": m.get("title"), "year": m.get("year"),
                    "path": m.get("path") or "", "size_bytes": size,
                    "has_file": bool(m.get("hasFile")),
                }))
        except ArrError as exc:
            errors.append(f"Radarr: {exc}")

    if do_tv:
        try:
            sc = SonarrClient(str(config.get("sonarr_url") or ""),
                              str(config.get("sonarr_api_key") or ""))
            for s in await sc.get_series():
                stats = s.get("statistics") or {}
                size = int(stats.get("sizeOnDisk") or 0)
                collected.append((size, {
                    "media_type": "tv", "arr_id": s.get("id"), "tmdb_id": s.get("tmdbId"),
                    "tvdb_id": s.get("tvdbId"), "title": s.get("title"), "year": s.get("year"),
                    "path": s.get("path") or "", "size_bytes": size,
                    "has_file": size > 0 or int(stats.get("episodeFileCount") or 0) > 0,
                }))
        except ArrError as exc:
            errors.append(f"Sonarr: {exc}")

    collected.sort(key=lambda t: t[0], reverse=True)
    top = collected[: max(1, int(limit))]
    _set(phase="scanning", total_items=len(top),
         total_bytes=sum(sz for sz, _ in top))
    total = 0
    seen_arr: set[tuple[Any, Any]] = set()
    for size, rec in top:
        has_file = rec.pop("has_file")
        seen_arr.add((rec["media_type"], rec.get("arr_id")))
        if empty_cleanup and not has_file:
            # No files on disk: remove the stale entry rather than show it as
            # "not deletable". Pre-selected — there's nothing on disk to lose.
            _mark_empty(rec)
            database.add_scan_item(scan_id, rec)
            _tick(rec.get("title") or "", size)
            continue
        path_ok, path_reason = check_path(rec["path"], roots) if has_file else (False, "no files on disk")
        eligible = has_file and path_ok
        rec.update({
            "score": None, "rating_source": "size",
            "proposed_action": ACTION_DELETE if eligible else ACTION_REVIEW,
            "prevent_redl": eligible,
            "reason": (f"{_gb(size)} GB" if eligible
                       else f"{_gb(size)} GB — not deletable ({path_reason})"),
            "requested_by": _requester(reqmap, rec["media_type"], rec.get("tmdb_id"), rec.get("tvdb_id")),
            "selected": False,
        })
        database.add_scan_item(scan_id, rec)
        total += size
        _tick(rec.get("title") or "", size)

    # When cleaning up empties, surface EVERY 0-byte entry (not just those that
    # happened to fall within the top-N) plus orphaned empty folders on disk.
    empty_n = empty_folders = 0
    if empty_cleanup:
        for size, rec in collected:
            if (rec["media_type"], rec.get("arr_id")) in seen_arr:
                continue
            if rec.get("has_file"):
                continue
            _mark_empty(rec)
            database.add_scan_item(scan_id, rec)
            empty_n += 1
        for root in roots:
            for d in find_empty_dirs(root):
                ok, _reason = check_path(d, roots)
                if not ok:
                    continue
                database.add_scan_item(scan_id, {
                    "media_type": "folder", "arr_id": None,
                    "title": os.path.basename(d) or d, "score": None,
                    "rating_source": "empty folder", "path": d, "size_bytes": 0,
                    "proposed_action": ACTION_DELETE, "prevent_redl": False,
                    "reason": "Orphaned empty folder (0 bytes) — will be removed from disk",
                    "selected": True,
                })
                empty_folders += 1

    summary = {
        "mode": "biggest", "scope": scope, "limit": int(limit),
        "count": len(top), "total_bytes": total, "scan_id": scan_id, "errors": errors,
        "empty_cleanup": empty_cleanup, "empty_items": empty_n,
        "empty_folders": empty_folders,
    }
    status = "completed" if not errors else "completed_with_errors"
    database.finish_scan(scan_id, status, summary)
    _set(running=False, phase="done", status=status)
    items = sorted(database.get_scan_items(scan_id),
                   key=lambda r: r.get("size_bytes") or 0, reverse=True)
    return {"scan_id": scan_id, "status": status, "summary": summary, "items": items}
