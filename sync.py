from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field

import requests

from kb_sync.client import KBEntry, KibanaClient
from kb_sync.collector import FileCollector
from kb_sync.config import AuthConfig, ResolvedTarget


@dataclass
class SyncResult:
    target: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: int = 0


class SyncEngine:
    def __init__(self, auth: AuthConfig, collector: FileCollector, logger: logging.Logger) -> None:
        self._auth = auth
        self._collector = collector
        self._logger = logger

    def sync_target(
        self,
        client: KibanaClient,
        target: ResolvedTarget,
        repo_name: str,
        desired_entries: dict[str, str],
        entry_public: bool,
    ) -> SyncResult:
        result = SyncResult(target=target.name)
        prefix = f"{repo_name}/"

        self._logger.info("[%s] Starting sync", target.name)
        existing = client.list_entries(target)

        existing_by_title: dict[str, KBEntry] = {
            e.title: e for e in existing if e.title.startswith(prefix)
        }
        self._logger.info("[%s] Existing entries with repo prefix: %d", target.name, len(existing_by_title))

        for title, text in desired_entries.items():
            current = existing_by_title.get(title)

            if current is None:
                new_id = self._collector.build_entry_id(repo_name, title)
                try:
                    client.save_entry(target, KBEntry(id=new_id, title=title, text=text, public=entry_public))
                    result.created += 1
                    self._logger.info("[%s] CREATED %s", target.name, title)
                except requests.RequestException:
                    result.errors += 1
                    self._logger.exception("[%s] CREATE failed for %s", target.name, title)
                continue

            if current.text == text and current.public == entry_public:
                result.unchanged += 1
                self._logger.info("[%s] UNCHANGED %s", target.name, title)
                continue

            try:
                client.save_entry(target, KBEntry(id=current.id, title=title, text=text, public=entry_public))
                result.updated += 1
                self._logger.info("[%s] UPDATED %s", target.name, title)
            except requests.RequestException:
                result.errors += 1
                self._logger.exception("[%s] UPDATE failed for %s", target.name, title)

        desired_titles = set(desired_entries.keys())
        for title, current in existing_by_title.items():
            if title in desired_titles:
                continue
            try:
                client.delete_entry(target, current.id)
                result.deleted += 1
                self._logger.info("[%s] DELETED %s", target.name, title)
            except requests.RequestException:
                result.errors += 1
                self._logger.exception("[%s] DELETE failed for %s", target.name, title)

        self._logger.info(
            "[%s] Finished sync: created=%d updated=%d unchanged=%d deleted=%d errors=%d",
            target.name, result.created, result.updated, result.unchanged, result.deleted, result.errors,
        )
        return result

    def sync_all(
        self,
        targets: list[ResolvedTarget],
        repo_name: str,
        desired_entries: dict[str, str],
        entry_public: bool,
        max_workers: int,
    ) -> list[SyncResult]:
        results: list[SyncResult] = []

        if not targets:
            return results

        def sync_job(target: ResolvedTarget) -> SyncResult:
            client = KibanaClient(self._auth, target.verify_ssl, self._logger)
            self._logger.info("[%s] Worker started with auth mode: %s", target.name, client.auth_mode)
            return self.sync_target(client, target, repo_name, desired_entries, entry_public)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kb-sync") as executor:
            future_to_target = {executor.submit(sync_job, t): t for t in targets}
            for future in concurrent.futures.as_completed(future_to_target):
                target = future_to_target[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    self._logger.exception("[%s] Unhandled sync error: %s", target.name, exc)
                    results.append(SyncResult(target=target.name, errors=1))

        return results
