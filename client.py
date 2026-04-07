from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

from kb_sync.config import AuthConfig, KibanaTargetConfig, ResolvedTarget


@dataclass(frozen=True)
class KBEntry:
    id: str
    title: str
    text: str
    public: bool


class KibanaClient:
    def __init__(self, auth: AuthConfig, verify_ssl: bool, logger: logging.Logger) -> None:
        self._logger = logger
        self._session, self._auth_mode = self._build_session(auth, verify_ssl)

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    @staticmethod
    def _build_session(auth: AuthConfig, verify_ssl: bool) -> tuple[requests.Session, str]:
        session = requests.Session()
        session.headers.update({
            "kbn-xsrf": "true",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-elastic-internal-origin": "kibana",
        })

        if not verify_ssl:
            session.verify = False
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

        kibana_version = os.getenv("KIBANA_VERSION")
        if kibana_version:
            session.headers["kbn-version"] = kibana_version

        api_key = os.getenv(auth.api_key_env) or os.getenv(auth.fallback_api_key_env)
        username = os.getenv(auth.username_env)
        password = os.getenv(auth.password_env)

        if auth.mode == "api_key" or (auth.mode == "auto" and api_key):
            if not api_key:
                raise ValueError(f"Missing API key in env vars: {auth.api_key_env} / {auth.fallback_api_key_env}")
            session.headers["Authorization"] = f"ApiKey {api_key.strip()}"
            return session, "api_key"

        if auth.mode == "basic" or auth.mode == "auto":
            if not username or not password:
                raise ValueError(f"Missing basic auth env vars: {auth.username_env} / {auth.password_env}")
            session.auth = (username, password)
            return session, "basic"

        raise ValueError(f"Unsupported auth mode: {auth.mode}")

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: int,
        retries: int,
        retry_delay: float,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        call_name: str = "",
    ) -> requests.Response:
        max_attempts = 1 + retries

        for attempt in range(1, max_attempts + 1):
            try:
                self._logger.info(
                    "%s attempt %d/%d -> %s %s",
                    call_name,
                    attempt,
                    max_attempts,
                    method,
                    url,
                )
                response = self._session.request(
                    method, url, params=params, json=body, timeout=timeout,
                )
                self._logger.info("%s -> HTTP %s", call_name, response.status_code)
                return response
            except requests.RequestException as exc:
                self._logger.warning("%s failed on attempt %d: %s", call_name, attempt, exc)
                if attempt < max_attempts:
                    time.sleep(retry_delay)
                    continue
                raise

        raise RuntimeError("Unexpected retry state")

    def _target_request(
        self,
        target: ResolvedTarget,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        call_name: str = "",
    ) -> requests.Response:
        url = f"{target.base_url}/s/{target.space_id}{path}"
        return self._request(
            method,
            url,
            timeout=target.timeout_seconds,
            retries=target.http_retries,
            retry_delay=target.http_retry_delay_seconds,
            params=params,
            body=body,
            call_name=f"[{target.name}] {call_name}",
        )

    def list_space_ids(self, target_config: KibanaTargetConfig) -> list[str]:
        if isinstance(target_config.spaces, list):
            self._logger.info("[%s] Spaces configured explicitly: %s", target_config.name, ", ".join(target_config.spaces))
            return target_config.spaces

        if target_config.spaces.strip().lower() != "all":
            self._logger.info("[%s] Single space configured: %s", target_config.name, target_config.spaces)
            return [target_config.spaces]

        url = f"{target_config.base_url}/api/spaces/space"
        self._logger.info("[%s] Discovering all spaces via %s", target_config.name, url)

        response = self._request(
            "GET",
            url,
            timeout=target_config.timeout_seconds,
            retries=target_config.http_retries,
            retry_delay=target_config.http_retry_delay_seconds,
            call_name=f"[{target_config.name}] List spaces",
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"Unexpected spaces API response format: expected list, got {type(payload).__name__}")

        space_ids = [
            str(item["id"]).strip()
            for item in payload
            if isinstance(item, dict) and item.get("id")
        ]
        if not space_ids:
            raise ValueError("No spaces returned by spaces API")

        self._logger.info("[%s] Discovered %d spaces: %s", target_config.name, len(space_ids), ", ".join(space_ids))
        return space_ids

    def list_entries(self, target: ResolvedTarget, query: str = "") -> list[KBEntry]:
        all_entries: list[KBEntry] = []
        page = 1
        per_page = 100

        while True:
            params: dict[str, Any] = {
                "query": query,
                "sortBy": "title",
                "sortDirection": "asc",
                "page": page,
                "perPage": per_page,
            }

            response = self._target_request(
                target, "GET",
                "/internal/observability_ai_assistant/kb/entries",
                params=params,
                call_name=f"List entries page {page}",
            )
            response.raise_for_status()

            payload = response.json()
            raw_entries = payload.get("entries", [])
            if not isinstance(raw_entries, list) or not raw_entries:
                break

            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                entry_id = raw.get("id", "")
                title = raw.get("title", "")
                if not entry_id or not title:
                    continue
                all_entries.append(KBEntry(
                    id=str(entry_id),
                    title=str(title),
                    text=str(raw.get("text", "")),
                    public=bool(raw.get("public", False)),
                ))

            total = payload.get("total")
            if isinstance(total, int) and len(all_entries) >= total:
                break
            if len(raw_entries) < per_page:
                break

            page += 1
            if page > 1000:
                self._logger.warning("[%s] Pagination stopped after 1000 pages", target.name)
                break

        return all_entries

    def save_entry(self, target: ResolvedTarget, entry: KBEntry) -> None:
        response = self._target_request(
            target, "POST",
            "/internal/observability_ai_assistant/kb/entries/save",
            body={"id": entry.id, "title": entry.title, "text": entry.text, "public": entry.public},
            call_name=f"Save entry {entry.title}",
        )
        response.raise_for_status()

    def delete_entry(self, target: ResolvedTarget, entry_id: str) -> None:
        response = self._target_request(
            target, "DELETE",
            f"/internal/observability_ai_assistant/kb/entries/{entry_id}",
            call_name=f"Delete entry {entry_id}",
        )
        response.raise_for_status()
