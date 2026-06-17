"""
Path safety guardrails.

This module is the single source of truth for deciding whether a filesystem
path is allowed to be deleted. Everything here is intentionally conservative:
when in doubt, say no.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# Paths that must never be treated as deletable media roots or deletion targets,
# regardless of configuration.
FORBIDDEN_PATHS = {
    "",
    "/",
    "/app",
    "/config",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
}


def normalize(path: str | os.PathLike[str]) -> str:
    """Return an absolute, symlink-resolved, normalized path string.

    Uses os.path.realpath so that symlink trickery cannot smuggle a deletion
    outside an allowed root.
    """
    if path is None:
        return ""
    p = str(path).strip()
    if not p:
        return ""
    return os.path.realpath(os.path.abspath(p))


def parse_media_roots(raw: str | Iterable[str] | None) -> list[str]:
    """Parse a MEDIA_ROOTS value (comma- or colon-separated, or a list) into a
    cleaned list of safe absolute roots. Forbidden roots are dropped.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        # Support both comma and OS pathsep separators.
        parts: list[str] = []
        for chunk in raw.split(","):
            parts.extend(chunk.split(os.pathsep))
        candidates = parts
    else:
        candidates = list(raw)

    roots: list[str] = []
    for c in candidates:
        norm = normalize(c)
        if not norm:
            continue
        if norm in FORBIDDEN_PATHS:
            continue
        if norm not in roots:
            roots.append(norm)
    return roots


def is_within(child: str, parent: str) -> bool:
    """True if `child` is `parent` or lives underneath it (both normalized)."""
    if not child or not parent:
        return False
    child_n = normalize(child)
    parent_n = normalize(parent)
    if not child_n or not parent_n:
        return False
    try:
        # commonpath raises ValueError on different drives / empty input.
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        return False


def check_path(path: str, media_roots: Iterable[str]) -> tuple[bool, str]:
    """Decide whether `path` may be deleted.

    Returns (allowed, reason). `reason` explains a denial, or is "ok" when
    allowed.
    """
    norm = normalize(path)
    if not norm:
        return False, "empty or unresolved path"
    if norm in FORBIDDEN_PATHS:
        return False, f"refusing to act on protected path: {norm}"

    roots = [r for r in media_roots if r]
    if not roots:
        return False, "no media roots configured"

    for root in roots:
        if root in FORBIDDEN_PATHS:
            # A misconfigured root must never authorize a deletion.
            continue
        if is_within(norm, root):
            # Never allow deleting a root itself, only things strictly inside it.
            if norm == normalize(root):
                return False, f"refusing to delete a media root itself: {norm}"
            return True, "ok"

    return False, f"path is outside the allowed media roots: {norm}"


def safe_to_delete(path: str, media_roots: Iterable[str]) -> bool:
    allowed, _ = check_path(path, media_roots)
    return allowed
