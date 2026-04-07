from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.sync import SyncResult


class ReportWriter:
    def __init__(self, report_path: Path = Path("kibana_kb_sync_report.md")) -> None:
        self._report_path = report_path

    def write(
        self,
        results: list[SyncResult],
        repo_name: str,
        repo_dir: Path,
        log_path: Path,
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        has_errors = any(r.errors > 0 for r in results)

        lines: list[str] = [
            "# Knowledge Base Sync Report",
            "",
            f"- Time: {now}",
            f"- Repository: {repo_name}",
            f"- Local clone: {repo_dir}",
            f"- Overall result: {'FAIL' if has_errors else 'PASS'}",
            "",
            "| Target | Created | Updated | Unchanged | Deleted | Errors |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in results:
            lines.append(f"| {r.target} | {r.created} | {r.updated} | {r.unchanged} | {r.deleted} | {r.errors} |")

        self._report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        print("\n".join(lines))
        print(f"Markdown report written to {self._report_path}")
        print(f"Detailed logs written to {log_path}")
