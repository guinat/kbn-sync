from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SourceConfig:
    repo_url: str
    clone_dir: Path
    include_extensions: frozenset[str]
    exclude_globs: tuple[str, ...]
    public: bool
    max_workers: int
    git_ssh_command: str | None


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    username_env: str
    password_env: str
    api_key_env: str
    fallback_api_key_env: str


@dataclass(frozen=True)
class ResolvedTarget:
    name: str
    base_url: str
    space_id: str
    verify_ssl: bool
    timeout_seconds: int
    http_retries: int
    http_retry_delay_seconds: float


@dataclass(frozen=True)
class KibanaTargetConfig:
    name: str
    base_url: str
    verify_ssl: bool
    timeout_seconds: int
    http_retries: int
    http_retry_delay_seconds: float
    spaces: list[str] | str

    def resolve_target(self, space_id: str) -> ResolvedTarget:
        return ResolvedTarget(
            name=f"{self.name}:{space_id}",
            base_url=self.base_url,
            space_id=space_id,
            verify_ssl=self.verify_ssl,
            timeout_seconds=self.timeout_seconds,
            http_retries=self.http_retries,
            http_retry_delay_seconds=self.http_retry_delay_seconds,
        )


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    auth: AuthConfig
    targets: list[KibanaTargetConfig]


def _resolve_git_ssh_command(source_raw: dict[str, Any]) -> str | None:
    ssh_command = str(source_raw.get("git_ssh_command", "")).strip()
    if ssh_command:
        return ssh_command

    env_var = str(source_raw.get("git_ssh_command_env", "GIT_SSH_COMMAND"))
    ssh_command = os.getenv(env_var, "").strip()
    return ssh_command or None


def _parse_source(source_raw: dict[str, Any]) -> SourceConfig:
    repo_url_env = str(source_raw.get("repo_url_env", "GITHUB_REPO"))
    repo_url = os.getenv(repo_url_env)
    if not repo_url:
        raise ValueError(f"Missing source repository URL in env var: {repo_url_env}")

    raw_extensions = source_raw.get("include_extensions", [".md"])
    include_extensions = frozenset(
        str(ext).strip().lower() for ext in raw_extensions if str(ext).strip()
    ) or frozenset({".md"})

    exclude_globs = tuple(
        str(p).strip() for p in source_raw.get("exclude_globs", []) if str(p).strip()
    )

    return SourceConfig(
        repo_url=repo_url,
        clone_dir=Path(str(source_raw.get("clone_dir", ".cache/repos"))),
        include_extensions=include_extensions,
        exclude_globs=exclude_globs,
        public=bool(source_raw.get("public", True)),
        max_workers=max(1, int(source_raw.get("max_workers", 8))),
        git_ssh_command=_resolve_git_ssh_command(source_raw),
    )


def _parse_auth(auth_raw: dict[str, Any]) -> AuthConfig:
    return AuthConfig(
        mode=str(auth_raw.get("mode", "auto")).strip().lower(),
        username_env=str(auth_raw.get("username_env", "KIBANA_USERNAME")),
        password_env=str(auth_raw.get("password_env", "KIBANA_PASSWORD")),
        api_key_env=str(auth_raw.get("api_key_env", "KIBANA_API_KEY")),
        fallback_api_key_env=str(auth_raw.get("fallback_api_key_env", "ELASTIC_API_KEY")),
    )


def _parse_targets(kibana_raw: list[dict[str, Any]]) -> list[KibanaTargetConfig]:
    targets: list[KibanaTargetConfig] = []

    for item in kibana_raw:
        if not isinstance(item, dict):
            continue

        base_url = str(item.get("base_url", "")).rstrip("/")
        if not base_url:
            continue

        spaces_raw = item.get("spaces", ["default"])
        if isinstance(spaces_raw, str):
            spaces: list[str] | str = spaces_raw.strip() or "default"
        elif isinstance(spaces_raw, list):
            spaces = [str(s).strip() for s in spaces_raw if str(s).strip()] or ["default"]
        else:
            spaces = ["default"]

        targets.append(
            KibanaTargetConfig(
                name=str(item.get("name", base_url)),
                base_url=base_url,
                verify_ssl=bool(item.get("verify_ssl", True)),
                timeout_seconds=int(item.get("timeout_seconds", 30)),
                http_retries=int(item.get("http_retries", 2)),
                http_retry_delay_seconds=float(item.get("http_retry_delay_seconds", 1.5)),
                spaces=spaces,
            )
        )

    if not targets:
        raise ValueError("No valid Kibana targets found in config")
    return targets


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("YAML config must be an object at top level")

    source_raw = raw.get("source", {})
    if not isinstance(source_raw, dict):
        source_raw = {}

    auth_raw = raw.get("auth", {})
    if not isinstance(auth_raw, dict):
        auth_raw = {}

    kibana_raw = raw.get("kibana")
    if not isinstance(kibana_raw, list) or not kibana_raw:
        raise ValueError("Config key 'kibana' must be a non-empty list")

    return AppConfig(
        source=_parse_source(source_raw),
        auth=_parse_auth(auth_raw),
        targets=_parse_targets(kibana_raw),
    )
