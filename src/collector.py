from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "kb"


class FileCollector:
    def __init__(self, include_extensions: frozenset[str], exclude_globs: tuple[str, ...]) -> None:
        self._include_extensions = include_extensions
        self._exclude_globs = exclude_globs

    def collect(self, repo_dir: Path, repo_name: str) -> dict[str, str]:
        entries: dict[str, str] = {}

        for path in repo_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._include_extensions:
                continue

            rel_path = path.relative_to(repo_dir).as_posix()
            if any(
                fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(path.name, pattern)
                for pattern in self._exclude_globs
            ):
                continue

            title = f"{repo_name}/{rel_path}"
            entries[title] = path.read_text(encoding="utf-8")

        return entries

    @staticmethod
    def build_entry_id(repo_name: str, space_id: str, title: str) -> str:
        digest = hashlib.sha1(f"{space_id}:{title}".encode("utf-8")).hexdigest()[:20]
        return f"kb-{_slugify(repo_name)}-{digest}"
