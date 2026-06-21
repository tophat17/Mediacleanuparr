"""Effective-settings resolution.

Environment variables provide the initial/default values (set via the Unraid
template or docker-compose). The web UI can override a subset of them; those
overrides live in the SQLite `settings` table and win over the environment.
"""
from __future__ import annotations

import os
from typing import Any

from . import database

# Keys the user may change from the web UI. Each maps to its env-var name.
UI_EDITABLE: dict[str, str] = {
    "radarr_url": "RADARR_URL",
    "radarr_api_key": "RADARR_API_KEY",
    "sonarr_url": "SONARR_URL",
    "sonarr_api_key": "SONARR_API_KEY",
    "tmdb_api_key": "TMDB_API_KEY",
    "min_rt_score": "MIN_RT_SCORE",
    "include_movies": "INCLUDE_MOVIES",
    "include_tv": "INCLUDE_TV",
    "include_unrated": "INCLUDE_UNRATED",
    "dry_run_only": "DRY_RUN_ONLY",
    "delete_files_enabled": "DELETE_FILES_ENABLED",
    "add_import_exclusion": "ADD_IMPORT_EXCLUSION",
    "sonarr_unmonitor": "SONARR_UNMONITOR",
    "auto_select_empty": "AUTO_SELECT_EMPTY",
    "seerr_url": "SEERR_URL",
    "seerr_api_key": "SEERR_API_KEY",
    "auto_unblock_on_request": "AUTO_UNBLOCK_ON_REQUEST",
    "seerr_webhook_token": "SEERR_WEBHOOK_TOKEN",
}

# Sensitive keys masked in API responses.
SECRET_KEYS = {"radarr_api_key", "sonarr_api_key", "tmdb_api_key", "seerr_api_key"}

DEFAULTS: dict[str, Any] = {
    "radarr_url": "",
    "radarr_api_key": "",
    "sonarr_url": "",
    "sonarr_api_key": "",
    "tmdb_api_key": "",
    "min_rt_score": 50,
    "include_movies": True,
    "include_tv": True,
    "include_unrated": False,
    "dry_run_only": True,
    "delete_files_enabled": False,
    "add_import_exclusion": True,
    # Sonarr has no native import exclusion like Radarr. The only re-download
    # prevention we offer is unmonitoring the series (keeps it in Sonarr).
    "sonarr_unmonitor": True,
    # When on, scan auto-selects 0-byte entries (no files) for removal from
    # Radarr/Sonarr and lists orphaned empty folders to clean up.
    "auto_select_empty": False,
    # Optional Overseerr/Jellyseerr ("Seerr") integration. When set, deletions
    # also clear the item's request in Seerr so it stops auto-requeuing it
    # (it stays re-requestable manually).
    "seerr_url": "",
    "seerr_api_key": "",
    # When on, a Seerr re-request webhook lifts the block this app placed on a
    # title (removes the Radarr/Sonarr exclusion, re-monitors, re-adds & searches).
    "auto_unblock_on_request": False,
    # Shared token that must appear in the webhook URL (?token=...). Auto-generated.
    "seerr_webhook_token": "",
}

# Read-only infra settings (not editable from the UI).
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
MEDIA_ROOTS_RAW = os.environ.get("MEDIA_ROOTS", "/media")
APP_PORT = int(os.environ.get("APP_PORT", "8787"))
TZ = os.environ.get("TZ", "America/Edmonton")
RATING_CACHE_TTL_SECONDS = int(os.environ.get("RATING_CACHE_TTL", str(60 * 60 * 24 * 14)))


def _coerce(key: str, value: str) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    return value


def env_default(key: str) -> Any:
    """Default for a key, taken from env var if present else hardcoded default."""
    env_name = UI_EDITABLE.get(key)
    if env_name and env_name in os.environ:
        return _coerce(key, os.environ[env_name])
    return DEFAULTS.get(key)


def get(key: str) -> Any:
    """Effective value: DB override if present, otherwise env/default."""
    db_val = database.get_setting(key)
    if db_val is not None:
        return _coerce(key, db_val)
    return env_default(key)


def set_value(key: str, value: Any) -> None:
    if key not in UI_EDITABLE:
        raise KeyError(f"setting '{key}' is not user-editable")
    database.set_setting(key, str(value))


def effective(mask_secrets: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in UI_EDITABLE:
        val = get(key)
        if mask_secrets and key in SECRET_KEYS:
            out[key] = bool(val)  # report only whether a secret is set
        else:
            out[key] = val
    # Include read-only infra info for display.
    out["_media_roots"] = media_roots()
    out["_config_dir"] = CONFIG_DIR
    out["_app_port"] = APP_PORT
    out["_tz"] = TZ
    return out


def media_roots() -> list[str]:
    from .safety import parse_media_roots
    return parse_media_roots(MEDIA_ROOTS_RAW)


def seed_from_env() -> None:
    """On first boot, persist nothing automatically - env stays authoritative
    until the user explicitly saves from the UI. This function exists as a hook
    and intentionally does not write defaults into the DB.
    """
    return
