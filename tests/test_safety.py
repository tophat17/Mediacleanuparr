"""Tests for the path safety guardrails."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.safety import check_path, parse_media_roots, is_within, normalize, FORBIDDEN_PATHS


def test_forbidden_roots_never_allow():
    for bad in ["/", "/config", "/app", "", "/etc", "/usr"]:
        allowed, reason = check_path("/media/movies/Film/file.mkv", [bad])
        # A forbidden root cannot authorize anything.
        assert allowed is False, f"{bad} should not authorize deletion"


def test_path_inside_media_root_allowed():
    allowed, reason = check_path("/media/movies/Good Film (2020)/film.mkv", ["/media"])
    assert allowed is True
    assert reason == "ok"


def test_path_outside_media_root_denied():
    allowed, reason = check_path("/data/other/file.mkv", ["/media"])
    assert allowed is False
    assert "outside" in reason


def test_root_itself_not_deletable():
    allowed, reason = check_path("/media", ["/media"])
    assert allowed is False


def test_empty_path_denied():
    allowed, reason = check_path("", ["/media"])
    assert allowed is False
    allowed, reason = check_path("   ", ["/media"])
    assert allowed is False


def test_no_roots_configured_denies():
    allowed, reason = check_path("/media/movies/x.mkv", [])
    assert allowed is False
    assert "no media roots" in reason


def test_parse_media_roots_filters_forbidden():
    roots = parse_media_roots("/media,/config,/,/data/movies")
    assert "/media" in roots
    assert "/data/movies" in roots
    assert "/config" not in roots
    assert "/" not in roots


def test_parse_media_roots_dedup():
    roots = parse_media_roots("/media,/media")
    assert roots.count("/media") == 1


def test_parse_media_roots_colon_separated():
    roots = parse_media_roots("/media:/data/tv")
    assert "/media" in roots and "/data/tv" in roots


def test_traversal_cannot_escape_root(tmp_path):
    # A path that uses ../ to climb out of the root must be rejected after
    # normalization.
    root = str(tmp_path / "media")
    os.makedirs(root, exist_ok=True)
    escape = os.path.join(root, "..", "secret.txt")
    allowed, _ = check_path(escape, [root])
    assert allowed is False


def test_symlink_escape_rejected(tmp_path):
    # A symlink inside the root that points outside must not be deletable,
    # because we resolve realpath before checking containment.
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "real.mkv"
    target.write_text("x")
    link = root / "link.mkv"
    link.symlink_to(target)
    allowed, _ = check_path(str(link), [str(root)])
    assert allowed is False


def test_is_within_basic():
    assert is_within("/media/a/b", "/media") is True
    assert is_within("/media", "/media") is True
    assert is_within("/mediaX/a", "/media") is False  # prefix trick


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
