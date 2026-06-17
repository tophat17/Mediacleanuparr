"""Tests for empty-folder detection and safe removal."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.deleter import _dir_has_files, _remove_empty_folder
from app.scanner import find_empty_dirs


def _build(tmp_path):
    root = tmp_path / "media"
    (root / "movies" / "GoodFilm").mkdir(parents=True)
    (root / "movies" / "GoodFilm" / "f.mkv").write_text("x")
    (root / "movies" / "EmptyLeftover").mkdir(parents=True)
    (root / "tv" / "GoodShow").mkdir(parents=True)
    (root / "tv" / "GoodShow" / "e.mkv").write_text("x")
    (root / "tv" / "OldShow" / "Season01").mkdir(parents=True)  # nested-empty
    return str(root)


def test_find_empty_dirs_returns_maximal_empties(tmp_path):
    root = _build(tmp_path)
    found = set(find_empty_dirs(root))
    assert os.path.join(root, "movies", "EmptyLeftover") in found
    # OldShow is the top of an empty subtree, not Season01
    assert os.path.join(root, "tv", "OldShow") in found
    assert os.path.join(root, "tv", "OldShow", "Season01") not in found
    # folders with files are never reported
    assert os.path.join(root, "movies", "GoodFilm") not in found
    assert root not in found


def test_dir_has_files(tmp_path):
    root = _build(tmp_path)
    assert _dir_has_files(os.path.join(root, "movies", "GoodFilm")) is True
    assert _dir_has_files(os.path.join(root, "tv", "OldShow")) is False


def test_remove_empty_folder_guards(tmp_path):
    root = _build(tmp_path)
    roots = [root]
    # refuses a media root itself
    ok, _ = _remove_empty_folder(root, roots)
    assert ok is False
    # refuses a folder that still has files
    ok, _ = _remove_empty_folder(os.path.join(root, "movies", "GoodFilm"), roots)
    assert ok is False
    # removes a genuinely empty folder
    target = os.path.join(root, "movies", "EmptyLeftover")
    ok, detail = _remove_empty_folder(target, roots)
    assert ok is True and not os.path.exists(target)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
