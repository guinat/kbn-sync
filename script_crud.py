from __future__ import annotations
 
import datetime as dt
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any
 
import requests
from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning
 
 
def as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
 
 
def make_session() -> requests.Session:
    load_dotenv()
 
    session = requests.Session()
    session.headers.update(
        {
            "kbn-xsrf": "true",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-elastic-internal-origin": "kibana",
        }
    )
 
    verify_ssl = as_bool(os.getenv("KIBANA_VERIFY_SSL"), default=True)
    if not verify_ssl:
        session.verify = False
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
 
    kibana_version = os.getenv("KIBANA_VERSION")
    if kibana_version:
        session.headers["kbn-version"] = kibana_version
 
    api_key = os.getenv("KIBANA_API_KEY") or os.getenv("ELASTIC_API_KEY")
    username = os.getenv("KIBANA_USERNAME")
    password = os.getenv("KIBANA_PASSWORD")
 
    if api_key:
        session.headers["Authorization"] = f"ApiKey {api_key.strip()}"
        session.headers["X-Debug-Auth-Mode"] = "api_key"
    elif username and password:
        session.auth = (username, password)
        session.headers["X-Debug-Auth-Mode"] = "basic"
    else:
        raise ValueError("Set KIBANA_API_KEY (or ELASTIC_API_KEY) or KIBANA_USERNAME/KIBANA_PASSWORD")
 
    return session
 
 
def build_base_url() -> str:
    load_dotenv()
    base_url = os.environ["KIBANA_BASE_URL"].rstrip("/")
    space_id = os.getenv("KIBANA_SPACE_ID", "default").strip()
    return f"{base_url}/s/{space_id}"
 
 
def call(
    session: requests.Session,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    trace_lines: list[str] | None = None,
    call_name: str | None = None,
) -> requests.Response:
    url = f"{build_base_url()}{path}"
    timeout_seconds = int(os.getenv("KIBANA_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("KIBANA_HTTP_RETRIES", "2"))
    delay_seconds = float(os.getenv("KIBANA_HTTP_RETRY_DELAY_SECONDS", "1.5"))
 
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            response = session.request(method, url, params=params, json=body, timeout=timeout_seconds)
            if trace_lines is not None:
                trace_lines.append(f"\n=== CALL: {call_name or path} ===")
                trace_lines.append(f"REQUEST: {method} {response.url}")
                trace_lines.append(f"ATTEMPT: {attempt}/{retries + 1}")
                trace_lines.append(f"REQUEST_PARAMS: {json.dumps(params or {}, ensure_ascii=False)}")
                trace_lines.append(f"REQUEST_BODY: {json.dumps(body or {}, ensure_ascii=False)}")
                trace_lines.append(f"RESPONSE_STATUS: {response.status_code}")
                try:
                    trace_lines.append(
                        "RESPONSE_JSON:\n" + json.dumps(response.json(), ensure_ascii=False, indent=2)
                    )
                except ValueError:
                    trace_lines.append("RESPONSE_TEXT:\n" + response.text)
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt <= retries:
                time.sleep(delay_seconds)
                continue
            if trace_lines is not None:
                trace_lines.append(f"\n=== CALL: {call_name or path} ===")
                trace_lines.append(f"REQUEST: {method} {url}")
                trace_lines.append(f"ATTEMPT: {attempt}/{retries + 1}")
                trace_lines.append(f"REQUEST_PARAMS: {json.dumps(params or {}, ensure_ascii=False)}")
                trace_lines.append(f"REQUEST_BODY: {json.dumps(body or {}, ensure_ascii=False)}")
                trace_lines.append(f"REQUEST_ERROR: {type(exc).__name__}: {exc}")
            raise
 
    raise RuntimeError(f"Unexpected HTTP retry state: {last_error}")
 
 
def response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"value": payload}
    except ValueError:
        return {"raw": response.text}
 
 
def list_entries(
    session: requests.Session,
    query: str = "",
    *,
    trace_lines: list[str] | None = None,
    call_name: str | None = None,
) -> tuple[requests.Response, list[dict[str, Any]]]:
    resp = call(
        session,
        "GET",
        "/internal/observability_ai_assistant/kb/entries",
        params={"query": query, "sortBy": "title", "sortDirection": "asc"},
        trace_lines=trace_lines,
        call_name=call_name,
    )
    payload = response_json(resp)
    entries = payload.get("entries", []) if isinstance(payload.get("entries", []), list) else []
    return resp, entries
 
 
def save_entry(
    session: requests.Session,
    *,
    entry_id: str,
    title: str,
    text: str,
    public: bool,
    trace_lines: list[str] | None = None,
    call_name: str | None = None,
) -> requests.Response:
    return call(
        session,
        "POST",
        "/internal/observability_ai_assistant/kb/entries/save",
        body={
            "id": entry_id,
            "title": title,
            "text": text,
            "public": public,
        },
        trace_lines=trace_lines,
        call_name=call_name,
    )
 
 
def delete_entry(
    session: requests.Session,
    entry_id: str,
    *,
    trace_lines: list[str] | None = None,
    call_name: str | None = None,
) -> requests.Response:
    return call(
        session,
        "DELETE",
        f"/internal/observability_ai_assistant/kb/entries/{entry_id}",
        trace_lines=trace_lines,
        call_name=call_name,
    )
 
 
def find_entry(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None
 
 
@dataclass
class Step:
    name: str
    status_code: int
    ok: bool
    check: str
 
 
def wait_for_entry(
    session: requests.Session,
    *,
    entry_id: str,
    query: str,
    timeout_seconds: int = 90,
    interval_seconds: float = 2.0,
    trace_lines: list[str] | None = None,
    call_name: str | None = None,
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None, str]:
    deadline = time.time() + timeout_seconds
    last_error = ""
 
    while time.time() < deadline:
        try:
            resp, entries = list_entries(
                session,
                query=query,
                trace_lines=trace_lines,
                call_name=call_name,
            )
            if resp.status_code == 200:
                return resp.status_code, entries, find_entry(entries, entry_id), ""
            last_error = f"http_{resp.status_code}"
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
 
        time.sleep(interval_seconds)
 
    return -1, [], None, f"timeout_waiting_entry ({last_error or 'unknown'})"
 
 
def main() -> None:
    session = make_session()
    auth_mode = session.headers.get("X-Debug-Auth-Mode", "unknown")
    trace_lines: list[str] = [
        f"TIME: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"AUTH_MODE: {auth_mode}",
        f"BASE_URL: {build_base_url()}",
    ]
 
    run_id = uuid.uuid4().hex[:8]
    entry_id = f"kb-crud-{run_id}"
    title_v1 = f"CRUD Test {run_id}"
    text_v1 = "Created by CRUD test script"
    title_v2 = f"CRUD Test {run_id} Updated"
    text_v2 = "Updated by CRUD test script"
    entry_public = True
 
    steps: list[Step] = []
 
    # 1) Status
    try:
        status_resp = call(
            session,
            "GET",
            "/internal/observability_ai_assistant/kb/status",
            trace_lines=trace_lines,
            call_name="Status endpoint",
        )
        status_payload = response_json(status_resp)
        status_ok = status_resp.status_code == 200
        steps.append(
            Step(
                name="Status endpoint",
                status_code=status_resp.status_code,
                ok=status_ok,
                check=f"enabled={status_payload.get('enabled')} inferenceModelState={status_payload.get('inferenceModelState')}",
            )
        )
    except requests.RequestException as exc:
        steps.append(
            Step(
                name="Status endpoint",
                status_code=-1,
                ok=False,
                check=f"request_error={type(exc).__name__}: {exc}",
            )
        )
 
    # 2) Read all before
    try:
        list_before_resp, entries_before = list_entries(
            session,
            query="",
            trace_lines=trace_lines,
            call_name="Read entries before create",
        )
        steps.append(
            Step(
                name="Read entries before create",
                status_code=list_before_resp.status_code,
                ok=list_before_resp.status_code == 200,
                check=f"entries_count={len(entries_before)}",
            )
        )
    except requests.RequestException as exc:
        steps.append(
            Step(
                name="Read entries before create",
                status_code=-1,
                ok=False,
                check=f"request_error={type(exc).__name__}: {exc}",
            )
        )
 
    # 3) Create
    create_ok = False
    try:
        create_resp = save_entry(
            session,
            entry_id=entry_id,
            title=title_v1,
            text=text_v1,
            public=entry_public,
            trace_lines=trace_lines,
            call_name="Create entry",
        )
        create_payload = response_json(create_resp)
        create_ok = create_resp.status_code in {200, 201}
        steps.append(
            Step(
                name="Create entry",
                status_code=create_resp.status_code,
                ok=create_ok,
                check=f"response_keys={','.join(sorted(create_payload.keys())) or 'none'}",
            )
        )
    except requests.RequestException as exc:
        steps.append(
            Step(
                name="Create entry",
                status_code=-1,
                ok=False,
                check=f"request_error={type(exc).__name__}: {exc}",
            )
        )
 
    # 4) Read and verify create
    if create_ok:
        read_created_status, entries_created, created_entry, created_wait_error = wait_for_entry(
            session,
            entry_id=entry_id,
            query="",
            trace_lines=trace_lines,
            call_name="Read after create",
        )
        created_ok = (
            read_created_status == 200
            and created_entry is not None
            and created_entry.get("title") == title_v1
            and created_entry.get("text") == text_v1
        )
        steps.append(
            Step(
                name="Read and verify created entry",
                status_code=read_created_status,
                ok=created_ok,
                check=(
                    f"found={created_entry is not None} "
                    f"title_ok={created_entry is not None and created_entry.get('title') == title_v1} "
                    f"text_ok={created_entry is not None and created_entry.get('text') == text_v1} "
                    f"wait_error={created_wait_error or 'none'}"
                ),
            )
        )
    else:
        steps.append(
            Step(
                name="Read and verify created entry",
                status_code=0,
                ok=False,
                check="skipped_because_create_failed",
            )
        )
 
    # 5) Update (same save endpoint with same id)
    update_ok = False
    if create_ok:
        try:
            update_resp = save_entry(
                session,
                entry_id=entry_id,
                title=title_v2,
                text=text_v2,
                public=entry_public,
                trace_lines=trace_lines,
                call_name="Update entry",
            )
            update_payload = response_json(update_resp)
            update_ok = update_resp.status_code in {200, 201}
            steps.append(
                Step(
                    name="Update entry",
                    status_code=update_resp.status_code,
                    ok=update_ok,
                    check=f"response_keys={','.join(sorted(update_payload.keys())) or 'none'}",
                )
            )
        except requests.RequestException as exc:
            steps.append(
                Step(
                    name="Update entry",
                    status_code=-1,
                    ok=False,
                    check=f"request_error={type(exc).__name__}: {exc}",
                )
            )
    else:
        steps.append(
            Step(
                name="Update entry",
                status_code=0,
                ok=False,
                check="skipped_because_create_failed",
            )
        )
 
    # 6) Read and verify update
    if update_ok:
        read_updated_status, entries_updated, updated_entry, updated_wait_error = wait_for_entry(
            session,
            entry_id=entry_id,
            query="",
            trace_lines=trace_lines,
            call_name="Read after update",
        )
        updated_ok = (
            read_updated_status == 200
            and updated_entry is not None
            and updated_entry.get("title") == title_v2
            and updated_entry.get("text") == text_v2
        )
        steps.append(
            Step(
                name="Read and verify updated entry",
                status_code=read_updated_status,
                ok=updated_ok,
                check=(
                    f"found={updated_entry is not None} "
                    f"title_ok={updated_entry is not None and updated_entry.get('title') == title_v2} "
                    f"text_ok={updated_entry is not None and updated_entry.get('text') == text_v2} "
                    f"wait_error={updated_wait_error or 'none'}"
                ),
            )
        )
    else:
        steps.append(
            Step(
                name="Read and verify updated entry",
                status_code=0,
                ok=False,
                check="skipped_because_update_failed",
            )
        )
 
    # 7) Delete
    delete_ok = False
    try:
        delete_resp = delete_entry(
            session,
            entry_id,
            trace_lines=trace_lines,
            call_name="Delete entry",
        )
        delete_ok = delete_resp.status_code in {200, 204}
        steps.append(
            Step(
                name="Delete entry",
                status_code=delete_resp.status_code,
                ok=delete_ok,
                check="delete request sent",
            )
        )
    except requests.RequestException as exc:
        steps.append(
            Step(
                name="Delete entry",
                status_code=-1,
                ok=False,
                check=f"request_error={type(exc).__name__}: {exc}",
            )
        )
 
    # 8) Read and verify delete
    if delete_ok:
        read_deleted_status, entries_deleted, deleted_entry, deleted_wait_error = wait_for_entry(
            session,
            entry_id=entry_id,
            query="",
            trace_lines=trace_lines,
            call_name="Read after delete",
        )
        deleted_ok = read_deleted_status == 200 and deleted_entry is None
        steps.append(
            Step(
                name="Read and verify deleted entry",
                status_code=read_deleted_status,
                ok=deleted_ok,
                check=f"found_after_delete={deleted_entry is not None} wait_error={deleted_wait_error or 'none'}",
            )
        )
    else:
        steps.append(
            Step(
                name="Read and verify deleted entry",
                status_code=0,
                ok=False,
                check="skipped_because_delete_failed",
            )
        )
 
    all_ok = all(step.ok for step in steps)
    now = dt.datetime.now().isoformat(timespec="seconds")
 
    lines: list[str] = []
    lines.append("# CRUD Report")
    lines.append("")
    lines.append(f"- Time: {now}")
    lines.append(f"- Auth mode: {auth_mode}")
    lines.append(f"- Base URL: {build_base_url()}")
    lines.append(f"- Test entry id: {entry_id}")
    lines.append(f"- Overall result: {'PASS' if all_ok else 'FAIL'}")
    lines.append("")
    lines.append("| Step | HTTP | Result | Check |")
    lines.append("|---|---:|---|---|")
    for step in steps:
        result = "PASS" if step.ok else "FAIL"
        check = step.check.replace("|", "\\|")
        lines.append(f"| {step.name} | {step.status_code} | {result} | {check} |")
 
    report = "\n".join(lines) + "\n"
 
    report_path = "kibana_kb_crud_report.md"
    with open(report_path, "w", encoding="utf-8") as file:
        file.write(report)
 
    with open("output.txt", "w", encoding="utf-8") as file:
        file.write("\n".join(trace_lines) + "\n")
 
    print(report)
    print(f"Markdown report written to {report_path}")
    print("Detailed call logs written to output.txt")
 
 
if __name__ == "__main__":
    main()