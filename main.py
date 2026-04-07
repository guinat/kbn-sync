from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from kb_sync import (
    FileCollector,
    GitClient,
    KibanaClient,
    ReportWriter,
    SyncEngine,
    SyncResult,
    load_config,
    setup_logger,
)


def main() -> None:
    load_dotenv()
    logger, log_path = setup_logger()

    config = load_config(Path(os.getenv("KB_CONFIG_FILE", "config.yaml")))

    git = GitClient(ssh_command=config.source.git_ssh_command)
    repo_dir = git.ensure_repo(config.source.repo_url, config.source.clone_dir)
    repo_name = GitClient.repo_name_from_url(config.source.repo_url)

    collector = FileCollector(config.source.include_extensions, config.source.exclude_globs)
    desired_entries = collector.collect(repo_dir, repo_name)

    logger.info("Starting KB sync")
    logger.info("Repository: %s (%s)", repo_name, config.source.repo_url)
    logger.info("Local clone: %s", repo_dir)
    logger.info("Files to sync: %d", len(desired_entries))
    logger.info("Exclude globs: %s", ", ".join(config.source.exclude_globs) or "(none)")
    logger.info("SSH command enabled: %s", config.source.git_ssh_command is not None)
    logger.info("Max workers: %d", config.source.max_workers)

    resolved_targets = []
    results: list[SyncResult] = []

    for target_config in config.targets:
        client = KibanaClient(config.auth, target_config.verify_ssl, logger)
        logger.info("[%s] Auth mode: %s", target_config.name, client.auth_mode)

        try:
            space_ids = client.list_space_ids(target_config)
        except Exception as exc:
            results.append(SyncResult(target=f"{target_config.name}:space-discovery", errors=1))
            logger.exception("[%s] Space discovery failed: %s", target_config.name, exc)
            continue

        logger.info("[%s] Target spaces: %s", target_config.name, ", ".join(space_ids))
        resolved_targets.extend(target_config.resolve_target(sid) for sid in space_ids)

    logger.info("Resolved %d target-space jobs for sync", len(resolved_targets))

    engine = SyncEngine(config.auth, collector, logger)
    results.extend(engine.sync_all(
        resolved_targets, repo_name, desired_entries,
        config.source.public, config.source.max_workers,
    ))

    ReportWriter().write(results, repo_name, repo_dir, log_path)


if __name__ == "__main__":
    main()
