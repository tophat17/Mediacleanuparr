"""Tests for the deletion guard and a FastAPI smoke test."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MEDIA_ROOTS", "/media")
os.environ.setdefault("DRY_RUN_ONLY", "true")

from app import config, database, deleter  # noqa: E402


def _fresh_db():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    database.init_db(tmp)
    return tmp


def test_dry_run_only_refuses_deletion():
    _fresh_db()
    database.set_setting("dry_run_only", "true")
    result = asyncio.run(deleter.execute_deletion(scan_id=1, item_ids=[1]))
    assert result["ok"] is False
    assert "dry-run" in result["error"].lower()


def test_health_endpoint():
    _fresh_db()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_delete_requires_exact_confirmation():
    _fresh_db()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/delete", json={"scan_id": 1, "confirm": "delete", "item_ids": [1]})
        assert r.status_code == 400
        r = client.post("/api/delete", json={"scan_id": 1, "confirm": "DELETE", "item_ids": []})
        assert r.status_code == 400


def test_settings_roundtrip():
    _fresh_db()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/settings", json={"min_rt_score": 65})
        assert r.status_code == 200
        r = client.get("/api/settings")
        assert r.json()["min_rt_score"] == 65


def test_settings_rejects_bad_score():
    _fresh_db()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/settings", json={"min_rt_score": 250})
        assert r.status_code == 400


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
