"""FastAPI application and HTTP API."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, config, database, scanner, unblock
from .arr_client import ArrError, RadarrClient, SonarrClient
from .deleter import execute_deletion
from .ratings import test_tmdb
from .seerr_client import SeerrClient

# ----------------------------- logging ------------------------------------
LOG_PATH = Path(config.CONFIG_DIR) / "mediacleanuparr.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("mediacleanuparr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db(config.CONFIG_DIR)
    config.seed_from_env()
    log.info("mediacleanuparr %s started", __version__)
    log.info("media roots: %s", config.media_roots())
    log.info(
        "dry-run-only: %s | delete-files: %s",
        config.get("dry_run_only"),
        config.get("delete_files_enabled"),
    )
    yield


app = FastAPI(title="mediacleanuparr", version=__version__, lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


# ----------------------------- models -------------------------------------
class SettingsUpdate(BaseModel):
    radarr_url: Optional[str] = None
    radarr_api_key: Optional[str] = None
    sonarr_url: Optional[str] = None
    sonarr_api_key: Optional[str] = None
    tmdb_api_key: Optional[str] = None
    min_rt_score: Optional[int] = None
    include_movies: Optional[bool] = None
    include_tv: Optional[bool] = None
    include_unrated: Optional[bool] = None
    auto_select_empty: Optional[bool] = None
    dry_run_only: Optional[bool] = None
    delete_files_enabled: Optional[bool] = None
    add_import_exclusion: Optional[bool] = None
    sonarr_unmonitor: Optional[bool] = None
    seerr_url: Optional[str] = None
    seerr_api_key: Optional[str] = None
    auto_unblock_on_request: Optional[bool] = None


class TestConnection(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None


class ScanRequest(BaseModel):
    scope: str = "both"  # movies | tv | both


class BiggestRequest(BaseModel):
    scope: str = "movies"  # movies | both
    limit: int = 50
    empty_cleanup: bool = False


class SelectRequest(BaseModel):
    item_id: int
    selected: bool


class ExcludeRequest(BaseModel):
    media_type: str
    tmdb_id: Optional[int] = None
    tvdb_id: Optional[int] = None
    title: Optional[str] = None
    excluded: bool = True


class DeleteRequest(BaseModel):
    scan_id: int
    confirm: str
    item_ids: list[int]


# ----------------------------- routes -------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return config.effective(mask_secrets=True)


@app.post("/api/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    changed = []
    for key, value in body.model_dump(exclude_none=True).items():
        # Don't overwrite a secret with an empty string from the masked UI.
        if key in config.SECRET_KEYS and value == "":
            continue
        if key == "min_rt_score" and not (0 <= int(value) <= 100):
            raise HTTPException(400, "min_rt_score must be 0-100")
        config.set_value(key, value)
        changed.append(key)
    # Generate a webhook token the first time auto-unblock is switched on.
    if bool(config.get("auto_unblock_on_request")) and not str(config.get("seerr_webhook_token") or ""):
        config.set_value("seerr_webhook_token", secrets.token_urlsafe(24))
        changed.append("seerr_webhook_token")
    log.info("settings updated: %s", changed)
    return {"ok": True, "changed": changed, "settings": config.effective()}


async def _test(client: RadarrClient | SonarrClient) -> JSONResponse:
    try:
        result = await client.test_connection()
        return JSONResponse(result)
    except ArrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)


@app.post("/api/test/radarr")
async def test_radarr(body: TestConnection) -> JSONResponse:
    url = body.url or str(config.get("radarr_url"))
    key = body.api_key or str(config.get("radarr_api_key"))
    return await _test(RadarrClient(url, key, timeout=8.0))


@app.post("/api/test/sonarr")
async def test_sonarr(body: TestConnection) -> JSONResponse:
    url = body.url or str(config.get("sonarr_url"))
    key = body.api_key or str(config.get("sonarr_api_key"))
    return await _test(SonarrClient(url, key, timeout=8.0))


@app.post("/api/test/tmdb")
async def test_tmdb_endpoint(body: TestConnection) -> JSONResponse:
    key = body.api_key or str(config.get("tmdb_api_key"))
    return JSONResponse(await test_tmdb(key))


@app.post("/api/test/seerr")
async def test_seerr_endpoint(body: TestConnection) -> JSONResponse:
    url = body.url or str(config.get("seerr_url"))
    key = body.api_key or str(config.get("seerr_api_key"))
    return JSONResponse(await SeerrClient(url, key).test_connection())


_scan_task: Optional[asyncio.Task] = None


async def _run_scan_bg(scope: str) -> None:
    try:
        await scanner.run_dry_scan(scope)
    except Exception as exc:  # noqa: BLE001 - surface any failure via progress
        log.exception("scan failed")
        scanner.mark_error(str(exc))


@app.post("/api/scan")
async def scan(body: ScanRequest) -> dict[str, Any]:
    if body.scope not in ("movies", "tv", "both"):
        raise HTTPException(400, "scope must be movies, tv, or both")
    if not str(config.get("tmdb_api_key") or "").strip():
        raise HTTPException(
            400,
            "A TheMovieDB (TMDb) API key is required before scanning. Add one in "
            "Setup (free at themoviedb.org), test it, and save.",
        )
    if scanner.is_running():
        raise HTTPException(409, "A scan is already running.")
    log.info("starting dry scan scope=%s", body.scope)
    scanner.reset_progress(body.scope)
    global _scan_task
    _scan_task = asyncio.create_task(_run_scan_bg(body.scope))
    return {"status": "started", "scope": body.scope}


@app.get("/api/scan/progress")
def scan_progress() -> dict[str, Any]:
    return scanner.get_progress()


async def _run_biggest_bg(scope: str, limit: int, empty_cleanup: bool) -> None:
    try:
        await scanner.run_biggest_scan(scope, limit, empty_cleanup)
    except Exception as exc:  # noqa: BLE001
        log.exception("biggest scan failed")
        scanner.mark_error(str(exc))


@app.post("/api/biggest")
async def biggest(body: BiggestRequest) -> dict[str, Any]:
    if body.scope not in ("movies", "tv", "both"):
        raise HTTPException(400, "scope must be movies, tv, or both")
    if scanner.is_running():
        raise HTTPException(409, "A scan is already running.")
    limit = max(1, min(500, int(body.limit)))
    log.info("starting biggest-items scan scope=%s limit=%s empty=%s",
             body.scope, limit, body.empty_cleanup)
    scanner.reset_progress(f"biggest:{body.scope}")
    global _scan_task
    _scan_task = asyncio.create_task(
        _run_biggest_bg(body.scope, limit, body.empty_cleanup))
    return {"status": "started", "scope": body.scope}


@app.get("/api/scan/latest")
def scan_latest() -> dict[str, Any]:
    scan = database.latest_scan()
    if not scan:
        return {"scan": None, "items": []}
    return {"scan": scan, "items": database.get_scan_items(scan["id"])}


@app.get("/api/scan/{scan_id}")
def scan_detail(scan_id: int) -> dict[str, Any]:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "scan not found")
    return {"scan": scan, "items": database.get_scan_items(scan_id)}


@app.post("/api/scan/select")
def select_item(body: SelectRequest) -> dict[str, Any]:
    database.set_item_selected(body.item_id, body.selected)
    return {"ok": True}


@app.post("/api/exclude")
def exclude(body: ExcludeRequest) -> dict[str, Any]:
    if body.tmdb_id is None and body.tvdb_id is None:
        raise HTTPException(400, "need a tmdb_id or tvdb_id to exclude")
    if body.excluded:
        database.add_exclusion(body.media_type, body.tmdb_id, body.tvdb_id, body.title or "?")
    else:
        database.remove_exclusion(body.media_type, body.tmdb_id, body.tvdb_id)
    return {"ok": True, "excluded": body.excluded}


@app.get("/api/exclusions")
def exclusions() -> dict[str, Any]:
    return {"exclusions": database.list_exclusions()}


@app.delete("/api/exclusions/{excl_id}")
def remove_exclusion(excl_id: int) -> dict[str, Any]:
    database.remove_exclusion_by_id(excl_id)
    return {"ok": True}


@app.post("/api/delete")
async def delete(body: DeleteRequest) -> dict[str, Any]:
    if body.confirm != "DELETE":
        raise HTTPException(400, 'confirmation failed: you must type "DELETE" exactly')
    if not body.item_ids:
        raise HTTPException(400, "no items selected")
    log.warning("DELETE confirmed: scan=%s items=%s", body.scan_id, body.item_ids)
    result = await execute_deletion(body.scan_id, body.item_ids)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "deletion refused"))
    return result


@app.get("/api/logs")
def logs(limit: int = 200) -> dict[str, Any]:
    return {"actions": database.recent_actions(limit)}


@app.get("/api/reports")
def reports() -> dict[str, Any]:
    d = Path(config.CONFIG_DIR) / "reports"
    if not d.exists():
        return {"reports": []}
    files = sorted(d.glob("deletion-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"reports": [{"name": f.name, "size": f.stat().st_size} for f in files]}


@app.get("/api/reports/{name}")
def report_file(name: str) -> FileResponse:
    # Prevent path traversal: only serve plain filenames from the reports dir.
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    path = Path(config.CONFIG_DIR) / "reports" / name
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path)


def _parse_seerr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull media_type + tmdb/tvdb id out of an Overseerr/Jellyseerr webhook
    payload. Tolerant of layout differences between versions."""
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}

    def _int(*vals: Any) -> Optional[int]:
        for v in vals:
            if v in (None, "", "0", 0):
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    raw_type = str(media.get("media_type") or payload.get("media_type") or "").lower()
    tmdb = _int(media.get("tmdbId"), media.get("tmdb_id"),
                payload.get("media_tmdbid"), payload.get("tmdbId"))
    tvdb = _int(media.get("tvdbId"), media.get("tvdb_id"),
                payload.get("media_tvdbid"), payload.get("tvdbId"))
    if raw_type.startswith("movie"):
        media_type = "movie"
    elif raw_type in ("tv", "show", "series"):
        media_type = "tv"
    else:
        media_type = "tv" if tvdb else "movie"
    title = payload.get("subject") or media.get("title")
    return {"media_type": media_type, "tmdb_id": tmdb, "tvdb_id": tvdb, "title": title}


@app.post("/api/seerr/webhook")
async def seerr_webhook(request: Request, token: str = "") -> dict[str, Any]:
    expected = str(config.get("seerr_webhook_token") or "")
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(401, "invalid or missing webhook token")
    if not bool(config.get("auto_unblock_on_request")):
        return {"ok": True, "skipped": "auto-unblock is disabled"}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    info = _parse_seerr_payload(payload if isinstance(payload, dict) else {})
    if info["tmdb_id"] is None and info["tvdb_id"] is None:
        return {"ok": True, "skipped": "no media id in payload"}
    try:
        result = await unblock.unblock_title(info["media_type"], info["tmdb_id"],
                                             info["tvdb_id"], info.get("title"))
    except Exception as exc:  # noqa: BLE001 - never 500 a webhook
        log.exception("seerr webhook unblock failed")
        return {"ok": False, "error": str(exc)}
    log.info("seerr webhook: %s tmdb=%s tvdb=%s -> %s",
             info["media_type"], info["tmdb_id"], info["tvdb_id"], result)
    return {"ok": True, "media": info, **result}


@app.get("/api/blocks")
def blocks() -> dict[str, Any]:
    return {"blocks": database.list_blocks(active_only=True)}


# Static frontend (mounted last so /api/* wins).
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
