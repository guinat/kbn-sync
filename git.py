from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


class GitClient:
    def __init__(self, ssh_command: str | None = None) -> None:
        self._extra_env: dict[str, str] | None = (
            {"GIT_SSH_COMMAND": ssh_command} if ssh_command else None
        )

    def _run(self, args: list[str], cwd: Path | None = None) -> str:
        env = os.environ.copy()
        if self._extra_env:
            env.update(self._extra_env)

        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        return result.stdout.strip()

    def ensure_repo(self, repo_url: str, clone_root: Path) -> Path:
        repo_name = self.repo_name_from_url(repo_url)
        repo_dir = clone_root / repo_name
        clone_root.mkdir(parents=True, exist_ok=True)

        if repo_dir.exists() and not (repo_dir / ".git").exists():
            shutil.rmtree(repo_dir)

        if not repo_dir.exists():
            self._run(["clone", repo_url, str(repo_dir)])
            return repo_dir

        origin = self._run(["remote", "get-url", "origin"], cwd=repo_dir)
        if origin.strip().rstrip("/") != repo_url.strip().rstrip("/"):
            shutil.rmtree(repo_dir)
            self._run(["clone", repo_url, str(repo_dir)])
            return repo_dir

        self._run(["fetch", "--all", "--prune"], cwd=repo_dir)
        self._run(["pull", "--ff-only"], cwd=repo_dir)
        return repo_dir

    @staticmethod
    def repo_name_from_url(repo_url: str) -> str:
        parsed = urlparse(repo_url)
        name = Path(parsed.path).name if parsed.path else repo_url.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name
