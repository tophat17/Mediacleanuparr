"""Deletion executor.

Only ever runs after the user has reviewed a dry run and typed DELETE. Re-checks
every safety condition at execution time (never trusts the dry-run snapshot
blindly) and writes an audit report to /config/reports.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, database
from .arr_client import ArrError, RadarrClient, SonarrClient
from .safety import check_path
from .seerr_client import SeerrClient, SeerrError


def _reports_dir() -> Path:
    d = Path(config.CONFIG_DIR) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _delete_movie(item: dict[str, Any], add_excl: bool) -> dict[str, Any]:
    client = RadarrClient(str(config.get("radarr_url")), str(config.get("radarr_api_key")))
    await client.delete_movie(int(item["arr_id"]), delete_files=True, add_exclusion=add_excl)
    return {"action": f"radarr-delete(files=true, exclusion={add_excl})", "success": True}


async def _delete_series(item: dict[str, Any]) -> dict[str, Any]:
    """Delete the series' episode files, then (if the toggle is on) unmonitor it
    so it won't be re-downloaded. The series record stays in Sonarr.
    """
    client = SonarrClient(str(config.get("sonarr_url")), str(config.get("sonarr_api_key")))
    sid = int(item["arr_id"])
    do_unmonitor = bool(config.get("sonarr_unmonitor"))

    n = await client.delete_episode_files(sid)
    parts = [f"deleted {n} file(s)"]
    if do_unmonitor:
        await client.unmonitor_series(sid)
        parts.append("unmonitored")
    return {"action": "sonarr-" + "+".join(parts), "success": True}


async def _clear_in_seerr(scan_id: int, item: dict[str, Any]) -> str:
    """If Seerr is configured, delete the item's media record so Seerr stops
    tracking/auto-requeuing it (it stays re-requestable). Best-effort: never
    fails the deletion. Returns a short note for the result, or "" if Seerr is
    off / the item isn't tracked there.
    """
    url = str(config.get("seerr_url") or "").strip()
    key = str(config.get("seerr_api_key") or "").strip()
    if not (url and key):
        return ""
    media_type = item.get("media_type")
    title = item.get("title") or "?"
    client = SeerrClient(url, key)
    try:
        media_id = await client.find_media_id(media_type, item.get("tmdb_id"), item.get("tvdb_id"))
        if media_id is None:
            database.log_action(scan_id, media_type, item.get("arr_id"), title,
                                "seerr-skip", True, "not tracked in Seerr")
            return "not in Seerr"
        await client.delete_media(media_id)
        database.log_action(scan_id, media_type, item.get("arr_id"), title,
                            "seerr-clear", True, f"mediaId={media_id}")
        return "cleared in Seerr"
    except SeerrError as exc:
        database.log_action(scan_id, media_type, item.get("arr_id"), title,
                            "seerr-error", False, str(exc))
        return f"Seerr error: {exc}"


async def _remove_empty_arr(item: dict[str, Any], add_excl: bool) -> dict[str, Any]:
    """Remove a 0-byte entry from the arr (no files to delete)."""
    if item.get("media_type") == "movie":
        client = RadarrClient(str(config.get("radarr_url")), str(config.get("radarr_api_key")))
        await client.delete_movie(int(item["arr_id"]), delete_files=False, add_exclusion=add_excl)
        return {"action": f"radarr-remove-empty(exclusion={add_excl})", "success": True}
    client = SonarrClient(str(config.get("sonarr_url")), str(config.get("sonarr_api_key")))
    await client.delete_series(int(item["arr_id"]), delete_files=False, add_exclusion=add_excl)
    return {"action": f"sonarr-remove-empty(exclusion={add_excl})", "success": True}


def _dir_has_files(path: str) -> bool:
    for _dp, _dn, filenames in os.walk(path):
        if filenames:
            return True
    return False


def _remove_empty_folder(path: str, roots: list[str]) -> tuple[bool, str]:
    """Remove a directory only if it's within a media root, isn't a root, and
    contains no files anywhere beneath it."""
    if not path or not os.path.isdir(path):
        return False, "folder not found"
    ok, reason = check_path(path, roots)
    if not ok:
        return False, f"unsafe ({reason})"
    if _dir_has_files(path):
        return False, "no longer empty (contains files)"
    try:
        shutil.rmtree(path)
        return True, "removed"
    except OSError as exc:
        return False, str(exc)


async def execute_deletion(scan_id: int, item_ids: list[int]) -> dict[str, Any]:
    if bool(config.get("dry_run_only")):
        return {
            "ok": False,
            "error": "Dry-run-only mode is enabled. Turn it off in Settings to allow deletions.",
        }

    delete_files = bool(config.get("delete_files_enabled"))
    add_excl = bool(config.get("add_import_exclusion"))
    roots = config.media_roots()

    items = database.get_items_by_ids(item_ids)
    results: list[dict[str, Any]] = []
    freed = 0

    for item in items:
        title = item.get("title") or "?"
        media_type = item.get("media_type")
        arr_id = item.get("arr_id")
        path = item.get("path") or ""
        size = int(item.get("size_bytes") or 0)

        if item.get("proposed_action") != "delete":
            detail = "skipped: not proposed for deletion"
            database.log_action(scan_id, media_type, arr_id, title, "skip", False, detail)
            results.append({"id": item["id"], "title": title, "skipped": True, "detail": detail})
            continue

        # Orphaned empty folder: remove the directory itself (no arr/Seerr).
        if media_type == "folder":
            ok, detail = _remove_empty_folder(path, roots)
            database.log_action(scan_id, "folder", None, title,
                                "folder-remove" if ok else "folder-skip", ok, f"{detail}: {path}")
            results.append({"id": item["id"], "title": title,
                            "action": "removed empty folder" if ok else None,
                            "success": ok, **({} if ok else {"error": detail})})
            continue

        is_empty = size <= 0  # 0-byte arr entry (no files on disk)

        # File-bearing items require "delete files from disk" + a safe path.
        if not is_empty:
            if not delete_files:
                detail = "skipped: 'Delete files from disk' is off"
                database.log_action(scan_id, media_type, arr_id, title, "skip", False, detail)
                results.append({"id": item["id"], "title": title, "skipped": True, "detail": detail})
                continue
            ok, reason = check_path(path, roots)
            if not ok:
                detail = f"skipped: path unsafe ({reason})"
                database.log_action(scan_id, media_type, arr_id, title, "guard", False, detail)
                results.append({"id": item["id"], "title": title, "skipped": True, "detail": detail})
                continue

        try:
            if is_empty:
                res = await _remove_empty_arr(item, add_excl)
                # Tidy up the now-empty folder, best-effort.
                if path:
                    fok, fdetail = _remove_empty_folder(path, roots)
                    if fok:
                        res["action"] += "+folder-removed"
            elif media_type == "movie":
                res = await _delete_movie(item, add_excl)
                freed += size
            elif media_type == "tv":
                res = await _delete_series(item)
                freed += size
            else:
                raise ArrError(f"unknown media type {media_type}")
            database.log_action(
                scan_id, media_type, arr_id, title, res["action"], True, f"path={path}"
            )
            entry = {"id": item["id"], "title": title, "action": res["action"], "success": True}
            seerr_note = await _clear_in_seerr(scan_id, item)
            if seerr_note:
                entry["seerr"] = seerr_note
            results.append(entry)
        except ArrError as exc:
            database.log_action(scan_id, media_type, arr_id, title, "error", False, str(exc))
            results.append({"id": item["id"], "title": title, "success": False, "error": str(exc)})

    report = _write_report(scan_id, results, freed)
    return {
        "ok": True,
        "deleted": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if r.get("success") is False),
        "freed_bytes": freed,
        "report": report,
        "results": results,
    }


def _write_report(scan_id: int, results: list[dict[str, Any]], freed: int) -> dict[str, str]:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = _reports_dir()
    json_path = d / f"deletion-{scan_id}-{ts}.json"
    csv_path = d / f"deletion-{scan_id}-{ts}.csv"

    payload = {
        "scan_id": scan_id,
        "timestamp": time.time(),
        "freed_bytes": freed,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, indent=2))

    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item_id", "title", "action", "success", "error", "detail"])
        for r in results:
            writer.writerow(
                [
                    r.get("id"),
                    r.get("title"),
                    r.get("action", ""),
                    r.get("success", ""),
                    r.get("error", ""),
                    r.get("detail", r.get("file_delete_blocked", "")),
                ]
            )
    return {"json": str(json_path), "csv": str(csv_path)}
