"""Tests for auto-unblock logic and the Seerr webhook endpoint."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MEDIA_ROOTS", "/media")
os.environ.setdefault("DRY_RUN_ONLY", "true")

from app import config, database, unblock  # noqa: E402


def _fresh_db():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    database.init_db(tmp)
    return tmp


class FakeRadarr:
    def __init__(self, movie_present=False):
        self.calls = []
        self.exclusions = [{"id": 5, "tmdbId": 100}]
        self.movie_present = movie_present

    async def remove_exclusion_for_tmdb(self, tmdb_id):
        had = any(e["tmdbId"] == tmdb_id for e in self.exclusions)
        self.exclusions = [e for e in self.exclusions if e["tmdbId"] != tmdb_id]
        self.calls.append(("remove_excl", tmdb_id))
        return had

    async def get_movie_by_tmdb(self, tmdb_id):
        return {"id": 9} if self.movie_present else None

    async def set_movie_monitored(self, mid, monitored=True):
        self.calls.append(("monitor", mid))

    async def search_movie(self, mid):
        self.calls.append(("search", mid))

    async def lookup_movie(self, tmdb_id):
        return {"tmdbId": tmdb_id, "title": "X"}

    async def get_quality_profiles(self):
        return [{"id": 1}]

    async def get_root_folders(self):
        return [{"path": "/movies"}]

    async def add_movie(self, lookup, qp, rf, search=True):
        self.calls.append(("add", qp, rf, search))
        return {"id": 11}


class FakeSonarr:
    def __init__(self, series_present=True):
        self.calls = []
        self.series_present = series_present
        self.exclusions = [{"id": 3, "tvdbId": 200}]

    async def remove_exclusion_for_tvdb(self, tvdb_id):
        had = any(e["tvdbId"] == tvdb_id for e in self.exclusions)
        self.exclusions = [e for e in self.exclusions if e["tvdbId"] != tvdb_id]
        self.calls.append(("remove_excl", tvdb_id))
        return had

    async def get_series_by_tvdb(self, tvdb_id):
        return {"id": 7} if self.series_present else None

    async def monitor_series(self, sid):
        self.calls.append(("monitor", sid))

    async def search_series(self, sid):
        self.calls.append(("search", sid))

    async def lookup_series(self, tvdb_id):
        return {"tvdbId": tvdb_id, "title": "S", "seasons": []}

    async def get_quality_profiles(self):
        return [{"id": 1}]

    async def get_language_profiles(self):
        return [{"id": 2}]

    async def get_root_folders(self):
        return [{"path": "/tv"}]

    async def add_series(self, lookup, qp, rf, language_profile_id=None, search=True):
        self.calls.append(("add", qp, rf, language_profile_id, search))
        return {"id": 12}


def test_unblock_movie_readds_when_absent(monkeypatch):
    _fresh_db()
    database.add_block("movie", 100, None, "X", "radarr_exclusion")
    fake = FakeRadarr(movie_present=False)
    monkeypatch.setattr(unblock, "_radarr", lambda: fake)
    res = asyncio.run(unblock.unblock_title("movie", 100, None))
    assert res["unblocked"] is True
    assert ("remove_excl", 100) in fake.calls
    assert any(c[0] == "add" for c in fake.calls)
    assert database.active_blocks_for("movie", 100, None) == []  # deactivated


def test_unblock_movie_remonitors_when_present(monkeypatch):
    _fresh_db()
    database.add_block("movie", 100, None, "X", "radarr_exclusion")
    fake = FakeRadarr(movie_present=True)
    monkeypatch.setattr(unblock, "_radarr", lambda: fake)
    res = asyncio.run(unblock.unblock_title("movie", 100, None))
    assert res["unblocked"] is True
    assert ("monitor", 9) in fake.calls and ("search", 9) in fake.calls
    assert not any(c[0] == "add" for c in fake.calls)


def test_unblock_series_unmonitor_does_not_touch_exclusions(monkeypatch):
    _fresh_db()
    database.add_block("tv", 300, 200, "S", "sonarr_unmonitor")
    fake = FakeSonarr(series_present=True)
    monkeypatch.setattr(unblock, "_sonarr", lambda: fake)
    res = asyncio.run(unblock.unblock_title("tv", 300, 200))
    assert res["unblocked"] is True
    assert ("monitor", 7) in fake.calls and ("search", 7) in fake.calls
    assert not any(c[0] == "remove_excl" for c in fake.calls)


def test_unblock_noop_when_no_block(monkeypatch):
    _fresh_db()
    res = asyncio.run(unblock.unblock_title("movie", 999, None))
    assert res["unblocked"] is False


# ----------------------------- webhook ------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_webhook_rejects_bad_token():
    _fresh_db()
    config.set_value("seerr_webhook_token", "GOOD")
    config.set_value("auto_unblock_on_request", True)
    with _client() as c:
        r = c.post("/api/seerr/webhook?token=BAD", json={"media": {"tmdbId": 5}})
        assert r.status_code == 401


def test_webhook_skips_when_disabled():
    _fresh_db()
    config.set_value("seerr_webhook_token", "GOOD")
    config.set_value("auto_unblock_on_request", False)
    with _client() as c:
        r = c.post("/api/seerr/webhook?token=GOOD", json={"media": {"tmdbId": 5}})
        assert r.status_code == 200
        assert "disabled" in r.json().get("skipped", "")


def test_webhook_parses_and_calls_unblock(monkeypatch):
    _fresh_db()
    config.set_value("seerr_webhook_token", "GOOD")
    config.set_value("auto_unblock_on_request", True)

    captured = {}

    async def fake_unblock(media_type, tmdb_id, tvdb_id):
        captured.update(media_type=media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
        return {"unblocked": True, "actions": []}

    from app import unblock as unblock_mod
    monkeypatch.setattr(unblock_mod, "unblock_title", fake_unblock)

    with _client() as c:
        r = c.post("/api/seerr/webhook?token=GOOD",
                   json={"notification_type": "MEDIA_AUTO_APPROVED",
                         "media": {"media_type": "movie", "tmdbId": "555", "tvdbId": ""}})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
    assert captured == {"media_type": "movie", "tmdb_id": 555, "tvdb_id": None}


def test_fallback_clears_unrecorded_exclusion(monkeypatch):
    """No recorded block, but a matching Radarr exclusion exists -> cleared."""
    _fresh_db()
    fake = FakeRadarr(movie_present=False)  # has exclusion tmdbId 100
    monkeypatch.setattr(unblock, "_radarr", lambda: fake)
    res = asyncio.run(unblock.unblock_title("movie", 100, None))
    assert res["unblocked"] is True
    assert ("remove_excl", 100) in fake.calls
    assert any(c[0] == "add" for c in fake.calls)


def test_fallback_noop_when_no_matching_exclusion(monkeypatch):
    _fresh_db()
    fake = FakeRadarr(movie_present=False)  # exclusion is for 100, not 999
    monkeypatch.setattr(unblock, "_radarr", lambda: fake)
    res = asyncio.run(unblock.unblock_title("movie", 999, None))
    assert res["unblocked"] is False
    assert not any(c[0] == "add" for c in fake.calls)
