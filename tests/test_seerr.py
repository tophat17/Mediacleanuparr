"""Tests for the Seerr client basics."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.seerr_client import SeerrClient


def test_url_construction():
    c = SeerrClient("http://host:5055/", "key")
    assert c._url("media/5") == "http://host:5055/api/v1/media/5"
    assert c._url("/status") == "http://host:5055/api/v1/status"


def test_test_connection_without_url():
    c = SeerrClient("", "")
    result = asyncio.run(c.test_connection())
    assert result["ok"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
