"""Tests for empty-item & orphaned-folder cleanup in the biggest-items scan."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MEDIA_ROOTS", "/media")
os.environ.setdefault("DRY_RUN_ONLY", "true")

from app import config, database, scanner  # noqa: E402


class FakeRadarr:
    def __init__(self, *a, **k):
        pass

    async def get_movies(self):
        return [
            {"id": 1, "tmdbId": 11, "title": "Big Movie", "year": 2020,
             "path": "/media/Big Movie", "sizeOnDisk": 5_000_000_000, "hasFile": True},
            {"id": 2, "tmdbId": 12, "title": "Empty Movie", "year": 2021,
             "path": "/media/Empty Movie", "sizeOnDisk": 0, "hasFile": False},
        ]


class FakeSonarr:
    def __init__(self, *a, **k):
        pass

    async def get_series(self):
        return [
            {"id": 3, "tmdbId": 21, "tvdbId": 31, "title": "Empty Show", "year": 2022,
             "path": "/media/Empty Show",
             "statistics": {"sizeOnDisk": 0, "episodeFileCount": 0}},
        ]


def _setup(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    config.CONFIG_DIR = str(cfg)
    database.init_db(str(cfg))
    root = tmp_path / "mediaroot"
    (root / "OrphanFolder").mkdir(parents=True)  # empty -> orphaned
    monkeypatch.setattr(config, "MEDIA_ROOTS_RAW", str(root))
    monkeypatch.setattr(scanner, "RadarrClient", FakeRadarr)
    monkeypatch.setattr(scanner, "SonarrClient", FakeSonarr)
    return str(root)


def _by_title(items):
    return {it["title"]: it for it in items}


def test_empty_cleanup_on_flags_empties_and_orphans(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    res = asyncio.run(scanner.run_biggest_scan("both", limit=1, empty_cleanup=True))
    items = database.get_scan_items(res["scan_id"])
    by = _by_title(items)

    # Empty arr entries surfaced as deletable, pre-selected, re-download blocked.
    for t in ("Empty Movie", "Empty Show"):
        assert by[t]["proposed_action"] == "delete", t
        assert by[t]["selected"] == 1, t
        assert by[t]["prevent_redl"] == 1, t  # add_import_exclusion default True

    # Orphaned empty folder surfaced as a removable folder item, pre-selected.
    assert "OrphanFolder" in by
    assert by["OrphanFolder"]["media_type"] == "folder"
    assert by["OrphanFolder"]["proposed_action"] == "delete"
    assert by["OrphanFolder"]["selected"] == 1

    assert res["summary"]["empty_items"] == 2
    assert res["summary"]["empty_folders"] == 1


def test_empty_cleanup_off_leaves_empties_not_deletable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    res = asyncio.run(scanner.run_biggest_scan("both", limit=10, empty_cleanup=False))
    items = database.get_scan_items(res["scan_id"])
    by = _by_title(items)

    for t in ("Empty Movie", "Empty Show"):
        assert by[t]["proposed_action"] == "review", t
        assert by[t]["selected"] == 0, t
        assert "not deletable" in (by[t]["reason"] or "")

    # No orphaned-folder items when the toggle is off.
    assert not any(it["media_type"] == "folder" for it in items)
    assert res["summary"]["empty_folders"] == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
