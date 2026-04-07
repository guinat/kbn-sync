from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass

import requests

from src.client import KBEntry, KibanaClient
from src.collector import FileCollector
from src.config import AuthConfig, ResolvedTarget


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

        existing_by_title: dict[str, list[KBEntry]] = {}
        for entry in existing:
            if not entry.title.startswith(prefix):
                continue
            existing_by_title.setdefault(entry.title, []).append(entry)

        existing_count = sum(len(entries) for entries in existing_by_title.values())
        duplicate_count = sum(len(entries) - 1 for entries in existing_by_title.values() if len(entries) > 1)
        self._logger.info("[%s] Existing entries with repo prefix: %d", target.name, existing_count)
        if duplicate_count:
            self._logger.warning(
                "[%s] Found %d duplicate repo entries by title; they will be cleaned up",
                target.name,
                duplicate_count,
            )

        for title, text in desired_entries.items():
            desired_id = self._collector.build_entry_id(repo_name, target.space_id, title)
            entries_for_title = existing_by_title.get(title, [])
            current = next((entry for entry in entries_for_title if entry.id == desired_id), None)
            desired_ready = current is not None

            if current is None:
                try:
                    client.save_entry(target, KBEntry(id=desired_id, title=title, text=text, public=entry_public))
                    desired_ready = True
                    if entries_for_title:
                        result.updated += 1
                        self._logger.info("[%s] MIGRATED %s to space-scoped id", target.name, title)
                    else:
                        result.created += 1
                        self._logger.info("[%s] CREATED %s", target.name, title)
                except requests.RequestException:
                    result.errors += 1
                    if entries_for_title:
                        self._logger.exception("[%s] MIGRATION failed for %s", target.name, title)
                    else:
                        self._logger.exception("[%s] CREATE failed for %s", target.name, title)
                    continue
            elif current.text == text and current.public == entry_public:
                result.unchanged += 1
                self._logger.info("[%s] UNCHANGED %s", target.name, title)
            else:
                try:
                    client.save_entry(target, KBEntry(id=desired_id, title=title, text=text, public=entry_public))
                    desired_ready = True
                    result.updated += 1
                    self._logger.info("[%s] UPDATED %s", target.name, title)
                except requests.RequestException:
                    result.errors += 1
                    self._logger.exception("[%s] UPDATE failed for %s", target.name, title)
                    continue

            if not desired_ready:
                continue

            for duplicate in entries_for_title:
                if duplicate.id == desired_id:
                    continue
                try:
                    client.delete_entry(target, duplicate.id)
                    result.deleted += 1
                    self._logger.info("[%s] DELETED duplicate %s (%s)", target.name, title, duplicate.id)
                except requests.RequestException:
                    result.errors += 1
                    self._logger.exception("[%s] DELETE duplicate failed for %s (%s)", target.name, title, duplicate.id)

        desired_titles = set(desired_entries.keys())
        for title, entries_for_title in existing_by_title.items():
            if title in desired_titles:
                continue
            for current in entries_for_title:
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
