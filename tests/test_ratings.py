"""Tests for user-rating parsing and scan classification logic."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database
from app.ratings import _arr_user, _to_100, _vote_from, resolve_user_score
from app.scanner import _movie_action, ACTION_DELETE, ACTION_KEEP, ACTION_REVIEW


def _fresh_db():
    d = tempfile.mkdtemp()
    config.CONFIG_DIR = d
    database.init_db(d)
    return d


def test_to_100_scales():
    assert _to_100("8.5") == 85
    assert _to_100(6) == 60
    assert _to_100("0.0") == 0


def test_to_100_garbage():
    assert _to_100("N/A") is None
    assert _to_100(None) is None
    assert _to_100(25) is None  # out of 0-10 range


def test_arr_user_prefers_tmdb():
    # TMDb wins over IMDb when both present (TMDb-centric).
    assert _arr_user({"imdb": {"value": 8.0}, "tmdb": {"value": 5.0}}) == 50
    assert _arr_user({"imdb": {"value": 7.1}}) == 71  # imdb fallback
    assert _arr_user({"tmdb": 6.0}) == 60             # scalar shape


def test_arr_user_ignores_critics():
    assert _arr_user({"rottenTomatoes": {"value": 90}}) is None
    assert _arr_user({"metacritic": {"value": 80}}) is None


def test_arr_user_missing():
    assert _arr_user(None) is None
    assert _arr_user({}) is None


def test_vote_from_requires_votes():
    assert _vote_from({"vote_average": 7.3, "vote_count": 1200}) == 73
    assert _vote_from({"vote_average": 0, "vote_count": 0}) is None   # no votes -> no rating
    assert _vote_from({"vote_average": 8.0}) is None                 # missing vote_count
    assert _vote_from(None) is None


def test_resolve_uses_arr_first_no_network():
    _fresh_db()
    # arr rating present -> returned immediately, TMDb API never consulted
    score, source = asyncio.run(
        resolve_user_score("movie", tmdb_id=278, imdb_id="tt0111161",
                           arr_ratings={"tmdb": {"value": 7.0}}))
    assert score == 70
    assert source == "Radarr/Sonarr"


def test_resolve_unavailable_without_key():
    _fresh_db()
    # no arr rating and no TMDb key -> unavailable (never crashes)
    score, source = asyncio.run(resolve_user_score("movie", tmdb_id=278, arr_ratings={}))
    assert score is None
    assert source == "unavailable"


def test_action_below_threshold_deletes():
    action, _, prevent = _movie_action(30, 50)
    assert action == ACTION_DELETE and prevent is True


def test_action_at_threshold_keeps():
    assert _movie_action(50, 50)[0] == ACTION_KEEP


def test_action_above_threshold_keeps():
    assert _movie_action(90, 50)[0] == ACTION_KEEP


def test_action_missing_is_review():
    action, reason, prevent = _movie_action(None, 50)
    assert action == ACTION_REVIEW and prevent is False
    assert "skipped" in reason.lower()


def test_action_zero_is_review():
    assert _movie_action(0, 50)[0] == ACTION_REVIEW


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
