from __future__ import annotations

import csv
import json
import random
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from thundr_bot.adspower_client import AdsPowerCapabilityError, AdsPowerClient
from thundr_bot.config import BotConfig


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, list, dict)):
        return value
    text = str(value).strip()
    if text == "":
        return None
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        if text.startswith("{") or text.startswith("["):
            return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        if text.startswith("0") and text != "0" and not text.startswith("0."):
            raise ValueError
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_rpa_status_payload(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if not text:
        return {"status": "unknown", "raw": raw_output}
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "status" not in payload and "state" in payload:
                payload["status"] = payload.get("state")
            payload.setdefault("raw", raw_output)
            return payload
    except json.JSONDecodeError:
        pass
    lowered = text.lower()
    if lowered in {"success", "completed", "done", "ok", "closed"}:
        return {"status": "success", "raw": raw_output}
    if lowered in {"running", "pending", "queued", "in_progress"}:
        return {"status": "running", "raw": raw_output}
    if lowered in {"failed", "error", "timeout", "cancelled"}:
        return {"status": "failed", "raw": raw_output}
    return {"status": lowered, "raw": raw_output}


def _is_adspower_rate_limit_error(error: Exception | str) -> bool:
    return "too many request per second" in str(error).strip().lower()


def _is_adspower_delete_in_use_error(error: Exception | str) -> bool:
    lowered = str(error).strip().lower()
    return "being used by other users" in lowered and "cannot be deleted" in lowered


def _is_adspower_empty_delete_in_use_error(error: Exception | str) -> bool:
    lowered = " ".join(str(error).strip().lower().split())
    return "failed to delete profiles" in lowered and ": [] is being used by other users and cannot be deleted" in lowered


def _is_adspower_profile_quota_error(error: Exception | str) -> bool:
    lowered = str(error).strip().lower()
    return (
        "number of imported accounts exceeds the limit" in lowered
        or "delete some accounts and try again" in lowered
    )


def _set_nested_value(container: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = [part.strip() for part in dotted_key.split(".") if part.strip()]
    if not parts:
        return
    current = container
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _row_to_profile_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for raw_key, raw_value in row.items():
        key = str(raw_key).strip()
        if not key:
            continue
        parsed = _parse_scalar(raw_value)
        if parsed is None:
            continue
        _set_nested_value(payload, key, parsed)
    return payload


def _load_json_template_rows(source_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return [dict(item) for item in rows if isinstance(item, dict)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    raise ValueError(f"Unsupported JSON template format in {source_path}")


def _load_csv_template_rows(source_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with source_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            if not row:
                continue
            rows.append({str(key): value for key, value in row.items() if key is not None})
    return rows


def _xlsx_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        raw = workbook.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    namespace = {"a": root.tag.split("}")[0].strip("{")}
    values: list[str] = []
    for item in root.findall("a:si", namespace):
        text_parts = [node.text or "" for node in item.findall(".//a:t", namespace)]
        values.append("".join(text_parts))
    return values


def _xlsx_first_sheet_target(workbook: zipfile.ZipFile) -> str:
    workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
    namespace = {"a": workbook_xml.tag.split("}")[0].strip("{"), "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    sheets = workbook_xml.find("a:sheets", namespace)
    if sheets is None or not list(sheets):
        raise ValueError("Workbook does not contain any sheets")
    first_sheet = list(sheets)[0]
    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    if not rel_id:
        raise ValueError("Workbook sheet relationship ID is missing")

    rels_xml = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    rel_ns = {"a": rels_xml.tag.split("}")[0].strip("{")}
    for relation in rels_xml.findall("a:Relationship", rel_ns):
        if relation.attrib.get("Id") == rel_id:
            target = relation.attrib.get("Target")
            if not target:
                break
            return f"xl/{target.lstrip('/')}"
    raise ValueError("Could not resolve the first worksheet target in workbook relationships")


def _column_ref_to_index(cell_ref: str) -> int:
    letters = []
    for char in cell_ref:
        if char.isalpha():
            letters.append(char.upper())
        else:
            break
    index = 0
    for char in letters:
        index = (index * 26) + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def _load_xlsx_template_rows(source_path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(source_path) as workbook:
        shared_strings = _xlsx_shared_strings(workbook)
        sheet_target = _xlsx_first_sheet_target(workbook)
        sheet_xml = ET.fromstring(workbook.read(sheet_target))

    namespace = {"a": sheet_xml.tag.split("}")[0].strip("{")}
    rows_data: list[list[str]] = []
    for row_node in sheet_xml.findall(".//a:row", namespace):
        cells: dict[int, str] = {}
        for cell in row_node.findall("a:c", namespace):
            cell_ref = cell.attrib.get("r") or ""
            cell_type = cell.attrib.get("t")
            value_node = cell.find("a:v", namespace)
            inline_node = cell.find("a:is", namespace)
            raw_value = ""
            if value_node is not None and value_node.text is not None:
                raw_value = value_node.text
            elif inline_node is not None:
                text_parts = [node.text or "" for node in inline_node.findall(".//a:t", namespace)]
                raw_value = "".join(text_parts)
            if cell_type == "s" and raw_value:
                index = int(raw_value)
                raw_value = shared_strings[index] if 0 <= index < len(shared_strings) else raw_value
            cells[_column_ref_to_index(cell_ref)] = raw_value
        if not cells:
            continue
        width = max(cells) + 1
        rows_data.append([cells.get(column_index, "") for column_index in range(width)])

    if not rows_data:
        return []

    header_row = rows_data[0]
    rows: list[dict[str, Any]] = []
    for values in rows_data[1:]:
        row: dict[str, Any] = {}
        for column_index, header in enumerate(header_row):
            key = str(header or "").strip()
            if not key:
                continue
            row[key] = values[column_index] if column_index < len(values) else ""
        if row:
            rows.append(row)
    return rows


def load_profile_template_rows(template_source: str) -> list[dict[str, Any]]:
    source_path = Path(template_source).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"Profile template source not found: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix in {".json"}:
        rows = _load_json_template_rows(source_path)
    elif suffix in {".csv"}:
        rows = _load_csv_template_rows(source_path)
    elif suffix in {".xlsx"}:
        rows = _load_xlsx_template_rows(source_path)
    else:
        raise ValueError(
            "BOT_ADSPOWER_PROFILE_TEMPLATE_SOURCE must point to a .json, .csv, or .xlsx file"
        )
    if not rows:
        raise ValueError(f"No template rows were found in {source_path}")
    return rows


class BackupRefillBlockedError(RuntimeError):
    """Raised when refill cannot proceed without user intervention."""


class DynamicBackupPoolManager:
    def __init__(self, config: BotConfig, client: AdsPowerClient, stop_event: Any):
        self.config = config
        self.client = client
        self.stop_event = stop_event
        self.state_path = Path(config.backup_pool_state_path).expanduser()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._template_rows: list[dict[str, Any]] | None = None
        self._proxy_query_cache: dict[str, list[str]] = {}
        self._extension_category_id: str | None = None
        self._state: dict[str, Any] = {
            "available_backup_user_ids": [],
            "template_cursor": 0,
            "refill_batches": {},
        }
        self._load_state()

    @staticmethod
    def _sanitize_user_id_list(raw_values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
        sanitized: list[str] = []
        seen: set[str] = set()
        for raw_value in raw_values or []:
            user_id = str(raw_value).strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            sanitized.append(user_id)
        return sanitized

    @staticmethod
    def _batch_timestamp_value(batch: dict[str, Any]) -> str:
        return str(batch.get("updated_at_utc") or batch.get("created_at_utc") or "")

    @classmethod
    def _batch_progress_key(cls, batch: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
        return (
            len(batch.get("prepared_user_ids") or []),
            len(batch.get("created_profiles") or []),
            len(batch.get("failed_profiles") or []),
            len(batch.get("template_rows") or []),
            1 if bool(batch.get("delete_complete")) else 0,
            cls._batch_timestamp_value(batch),
        )

    def _compact_refill_batches_locked(self) -> tuple[int, int]:
        batches = self._state["refill_batches"]
        merged_active_batches = 0
        active_signatures: dict[tuple[Any, str, tuple[str, ...]], str] = {}

        for batch_id in list(batches):
            batch = batches.get(batch_id)
            if batch is None:
                continue
            status = str(batch.get("status") or "queued")
            if status in {"ready", "failed", "blocked"}:
                continue

            signature = (
                batch.get("proxy_label"),
                str(batch.get("reason") or "replacement"),
                tuple(self._sanitize_user_id_list(batch.get("retired_user_ids") or [])),
            )
            existing_batch_id = active_signatures.get(signature)
            if existing_batch_id is None:
                active_signatures[signature] = batch_id
                continue

            existing_batch = batches.get(existing_batch_id)
            if existing_batch is None:
                active_signatures[signature] = batch_id
                continue

            keep_batch_id = existing_batch_id
            drop_batch_id = batch_id
            keep_batch = existing_batch
            drop_batch = batch
            if self._batch_progress_key(batch) > self._batch_progress_key(existing_batch):
                keep_batch_id = batch_id
                drop_batch_id = existing_batch_id
                keep_batch = batch
                drop_batch = existing_batch

            keep_batch["requested_count"] = max(
                int(keep_batch.get("requested_count") or 0),
                int(drop_batch.get("requested_count") or 0),
            )
            if not keep_batch.get("error") and drop_batch.get("error"):
                keep_batch["error"] = drop_batch.get("error")
            keep_retry_after = keep_batch.get("retry_after_epoch")
            drop_retry_after = drop_batch.get("retry_after_epoch")
            if keep_retry_after is None:
                keep_batch["retry_after_epoch"] = drop_retry_after
            elif drop_retry_after is not None:
                keep_batch["retry_after_epoch"] = max(float(keep_retry_after), float(drop_retry_after))
            keep_batch["retry_attempts"] = max(
                int(keep_batch.get("retry_attempts") or 0),
                int(drop_batch.get("retry_attempts") or 0),
            )
            keep_batch["updated_at_utc"] = max(
                self._batch_timestamp_value(keep_batch),
                self._batch_timestamp_value(drop_batch),
            )

            batches[keep_batch_id] = keep_batch
            batches.pop(drop_batch_id, None)
            active_signatures[signature] = keep_batch_id
            merged_active_batches += 1

        terminal_batch_ids = [
            batch_id
            for batch_id, batch in batches.items()
            if str(batch.get("status") or "queued") in {"ready", "failed", "blocked"}
        ]
        keep_terminal_limit = 500
        pruned_terminal_batches = 0
        if len(terminal_batch_ids) > keep_terminal_limit:
            terminal_batch_ids.sort(
                key=lambda batch_id: self._batch_timestamp_value(batches[batch_id]),
                reverse=True,
            )
            for batch_id in terminal_batch_ids[keep_terminal_limit:]:
                batches.pop(batch_id, None)
                pruned_terminal_batches += 1

        return merged_active_batches, pruned_terminal_batches

    def _find_equivalent_active_batch_locked(
        self,
        *,
        retired_user_ids: list[str],
        proxy_label: str | None,
        reason: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        target_retired_user_ids = tuple(self._sanitize_user_id_list(retired_user_ids))
        target_reason = str(reason or "replacement")
        for batch_id, batch in self._state["refill_batches"].items():
            if str(batch.get("status") or "queued") in {"ready", "failed", "blocked"}:
                continue
            if batch.get("proxy_label") != proxy_label:
                continue
            if str(batch.get("reason") or "replacement") != target_reason:
                continue
            batch_retired_user_ids = tuple(
                self._sanitize_user_id_list(batch.get("retired_user_ids") or [])
            )
            if batch_retired_user_ids != target_retired_user_ids:
                continue
            return batch_id, batch
        return None, None

    def _discard_incomplete_batches_from_previous_run_locked(self) -> tuple[int, int]:
        discarded_batches = 0
        salvaged_ready_user_ids = 0
        for batch in self._state["refill_batches"].values():
            status = str(batch.get("status") or "queued")
            if status in {"ready", "failed", "blocked"}:
                continue

            prepared_user_ids = self._sanitize_user_id_list(batch.get("prepared_user_ids") or [])
            created_ready_user_ids = [
                str(profile.get("user_id") or "").strip()
                for profile in batch.get("created_profiles") or []
                if str(profile.get("prep_status") or "") == "ready"
                and str(profile.get("user_id") or "").strip()
            ]
            for user_id in self._sanitize_user_id_list(prepared_user_ids + created_ready_user_ids):
                if user_id not in self._state["available_backup_user_ids"]:
                    self._state["available_backup_user_ids"].append(user_id)
                    salvaged_ready_user_ids += 1

            batch["status"] = "failed"
            batch["error"] = (
                "Discarded incomplete refill batch from a previous bot run; "
                "startup does not resume in-flight refill work"
            )
            batch["retry_attempts"] = 0
            batch["retry_after_epoch"] = None
            batch["updated_at_utc"] = _utcnow_iso()
            discarded_batches += 1

        return discarded_batches, salvaged_ready_user_ids

    def start(self) -> None:
        if not self.config.dynamic_backup_refill_enabled:
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="dynamic-backup-refill",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._thread.join(timeout=5)

    def available_backup_user_ids(self) -> list[str]:
        with self._lock:
            self._normalize_available_backup_user_ids_locked()
            return list(self._state["available_backup_user_ids"])

    def reserve_backup_user_id(self, *, excluded_user_ids: list[str] | set[str] | tuple[str, ...] | None = None) -> str | None:
        with self._lock:
            self._normalize_available_backup_user_ids_locked()
            excluded = {
                str(user_id).strip()
                for user_id in (excluded_user_ids or [])
                if str(user_id).strip()
            }
            if excluded:
                self._state["available_backup_user_ids"] = [
                    user_id
                    for user_id in self._state["available_backup_user_ids"]
                    if user_id not in excluded
                ]
            queue = deque(self._state["available_backup_user_ids"])
            if not queue:
                self._persist_state_locked()
                return None
            user_id = queue.popleft()
            self._state["available_backup_user_ids"] = list(queue)
            self._persist_state_locked()
            return str(user_id)

    def return_backup_user_id(self, user_id: str) -> None:
        user_id = str(user_id).strip()
        if not user_id:
            return
        with self._lock:
            self._normalize_available_backup_user_ids_locked()
            if user_id not in self._state["available_backup_user_ids"]:
                self._state["available_backup_user_ids"].append(user_id)
                self._normalize_available_backup_user_ids_locked()
                self._persist_state_locked()

    def append_ready_backup_user_id(self, user_id: str, *, batch_id: str) -> None:
        user_id = str(user_id).strip()
        if not user_id:
            return
        with self._lock:
            self._normalize_available_backup_user_ids_locked()
            if user_id not in self._state["available_backup_user_ids"]:
                self._state["available_backup_user_ids"].append(user_id)
            batch = self._state["refill_batches"].get(batch_id)
            if batch is not None:
                prepared = list(batch.get("prepared_user_ids") or [])
                if user_id not in prepared:
                    prepared.append(user_id)
                    batch["prepared_user_ids"] = prepared
                batch["updated_at_utc"] = _utcnow_iso()
            self._normalize_available_backup_user_ids_locked()
            self._persist_state_locked()

    def invalidate_user_ids(
        self,
        user_ids: list[str] | set[str] | tuple[str, ...],
    ) -> list[str]:
        invalid_user_ids = {
            str(user_id).strip()
            for user_id in user_ids
            if str(user_id).strip()
        }
        if not invalid_user_ids:
            return []
        with self._lock:
            self._normalize_available_backup_user_ids_locked()
            removed_user_ids = [
                user_id
                for user_id in self._state["available_backup_user_ids"]
                if user_id in invalid_user_ids
            ]
            if not removed_user_ids:
                return []
            self._state["available_backup_user_ids"] = [
                user_id
                for user_id in self._state["available_backup_user_ids"]
                if user_id not in invalid_user_ids
            ]
            self._persist_state_locked()
            return removed_user_ids

    def enqueue_refill_batch(
        self,
        retired_user_ids: list[str] | None = None,
        *,
        requested_count: int | None = None,
        proxy_label: str | None,
        reason: str = "replacement",
    ) -> str | None:
        if not self.config.dynamic_backup_refill_enabled:
            return None
        retired_user_ids = retired_user_ids or []
        sanitized = self._sanitize_user_id_list(retired_user_ids)
        requested_count = max(
            len(sanitized),
            int(requested_count if requested_count is not None else len(sanitized)),
        )
        if requested_count <= 0 and not sanitized:
            return None

        now = _utcnow_iso()
        with self._lock:
            existing_batch_id, existing_batch = self._find_equivalent_active_batch_locked(
                retired_user_ids=sanitized,
                proxy_label=proxy_label,
                reason=str(reason or "replacement"),
            )
            if existing_batch_id is not None and existing_batch is not None:
                existing_batch["requested_count"] = max(
                    int(existing_batch.get("requested_count") or 0),
                    requested_count,
                )
                existing_batch["updated_at_utc"] = now
                self._persist_state_locked()
                return existing_batch_id

            if sanitized:
                self._state["available_backup_user_ids"] = [
                    user_id
                    for user_id in self._state["available_backup_user_ids"]
                    if user_id not in set(sanitized)
                ]
            batch_id = uuid.uuid4().hex[:12]
            batch = {
                "batch_id": batch_id,
                "status": "queued",
                "proxy_label": proxy_label,
                "reason": str(reason or "replacement"),
                "requested_count": requested_count,
                "retired_user_ids": sanitized,
                "delete_complete": not sanitized,
                "template_rows": [],
                "created_profiles": [],
                "prepared_user_ids": [],
                "failed_profiles": [],
                "created_at_utc": now,
                "updated_at_utc": now,
                "blocked_reason": None,
                "error": None,
                "retry_attempts": 0,
                "retry_after_epoch": None,
            }
            self._state["refill_batches"][batch_id] = batch
            self._normalize_available_backup_user_ids_locked()
            self._persist_state_locked()
        return batch_id

    def refill_batches_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(batch)) for batch in self._state["refill_batches"].values()]

    def refill_batch_status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._lock:
            for batch in self._state["refill_batches"].values():
                status = str(batch.get("status") or "queued")
                counts[status] = counts.get(status, 0) + 1
        return counts

    def has_incomplete_batches(self) -> bool:
        with self._lock:
            for batch in self._state["refill_batches"].values():
                if str(batch.get("status") or "queued") not in {"ready", "failed", "blocked"}:
                    return True
        return False

    def pending_backup_output_count(self) -> int:
        total = 0
        with self._lock:
            for batch in self._state["refill_batches"].values():
                status = str(batch.get("status") or "queued")
                if status in {"ready", "failed", "blocked"}:
                    continue
                requested_count = max(0, int(batch.get("requested_count") or 0))
                prepared_count = len(batch.get("prepared_user_ids") or [])
                failed_count = len(batch.get("failed_profiles") or [])
                total += max(0, requested_count - prepared_count - failed_count)
        return total

    def trim_excess_pending_output(self, max_pending_output: int) -> int:
        max_pending_output = max(0, int(max_pending_output))
        trimmed_batch_ids: list[str] = []
        with self._lock:
            pending_output = self.pending_backup_output_count()
            if pending_output <= max_pending_output:
                return 0

            candidates: list[tuple[tuple[int, str], str, int]] = []
            for batch_id, batch in self._state["refill_batches"].items():
                status = str(batch.get("status") or "queued")
                if status in {"ready", "failed", "blocked"}:
                    continue

                created_profiles = list(batch.get("created_profiles") or [])
                prepared_user_ids = list(batch.get("prepared_user_ids") or [])
                failed_profiles = list(batch.get("failed_profiles") or [])
                if created_profiles or prepared_user_ids or failed_profiles:
                    continue

                requested_count = max(0, int(batch.get("requested_count") or 0))
                if requested_count <= 0:
                    continue

                candidates.append(
                    (
                        (
                            0 if not bool(batch.get("delete_complete")) else 1,
                            self._batch_timestamp_value(batch),
                        ),
                        batch_id,
                        requested_count,
                    )
                )

            candidates.sort()
            for _sort_key, batch_id, remaining_output in candidates:
                if pending_output <= max_pending_output:
                    break
                batch = self._state["refill_batches"].get(batch_id)
                if batch is None:
                    continue
                batch["status"] = "failed"
                batch["error"] = (
                    "Trimmed stale zero-progress refill batch because persisted pending output "
                    "exceeded current backup demand"
                )
                batch["retry_attempts"] = 0
                batch["retry_after_epoch"] = None
                batch["updated_at_utc"] = _utcnow_iso()
                trimmed_batch_ids.append(batch_id)
                pending_output = max(0, pending_output - remaining_output)

            if not trimmed_batch_ids:
                return 0

            self._persist_state_locked()

        print(
            "Dynamic backup refill trimmed excess zero-progress batches: "
            f"count={len(trimmed_batch_ids)} target_pending_output={max_pending_output}"
        )
        return len(trimmed_batch_ids)

    def summary_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state_path": str(self.state_path),
                "dynamic_refill_enabled": self.config.dynamic_backup_refill_enabled,
                "available_backup_count": len(self._state["available_backup_user_ids"]),
                "available_backup_user_ids": list(self._state["available_backup_user_ids"]),
                "template_cursor": int(self._state.get("template_cursor") or 0),
                "refill_batch_status_counts": self.refill_batch_status_counts(),
                "refill_batches": self.refill_batches_snapshot(),
            }

    def startup_log_payload(self) -> str:
        counts = self.refill_batch_status_counts()
        return (
            f"dynamic_refill_enabled={self.config.dynamic_backup_refill_enabled} "
            f"live_backups={len(self.available_backup_user_ids())} "
            f"pending_backup_output={self.pending_backup_output_count()} "
            f"target_live_backups={self.config.target_ready_backup_count} "
            f"refill_batches={counts or {}} "
            f"state_path={self.state_path}"
        )

    def _load_state(self) -> None:
        with self._lock:
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                except Exception as error:  # noqa: BLE001
                    print(f"Failed to read backup pool state from {self.state_path}: {error}")
                    loaded = {}
                if isinstance(loaded, dict):
                    self._state["available_backup_user_ids"] = list(
                        loaded.get("available_backup_user_ids") or []
                    )
                    self._state["template_cursor"] = int(loaded.get("template_cursor") or 0)
                    self._state["refill_batches"] = dict(loaded.get("refill_batches") or {})
            else:
                self._state["available_backup_user_ids"] = list(self.config.backup_user_ids)

            env_seed = [item.strip() for item in self.config.backup_user_ids if item.strip()]
            known_ids = self._all_known_profile_ids_locked()
            merged_extras = [user_id for user_id in env_seed if user_id not in known_ids]
            if merged_extras:
                self._state["available_backup_user_ids"].extend(merged_extras)

            for batch_id, batch in list(self._state["refill_batches"].items()):
                normalized = self._normalize_batch(batch_id, batch)
                self._state["refill_batches"][batch_id] = normalized

            discarded_batches, salvaged_ready_user_ids = (
                self._discard_incomplete_batches_from_previous_run_locked()
            )
            if discarded_batches or salvaged_ready_user_ids:
                print(
                    "Dynamic backup refill discarded stale in-flight batches from the previous run: "
                    f"discarded_batches={discarded_batches} "
                    f"salvaged_ready_user_ids={salvaged_ready_user_ids}"
                )

            merged_active_batches, pruned_terminal_batches = self._compact_refill_batches_locked()
            if merged_active_batches or pruned_terminal_batches:
                print(
                    "Dynamic backup refill state compacted on load: "
                    f"merged_active_batches={merged_active_batches} "
                    f"pruned_terminal_batches={pruned_terminal_batches}"
                )

            retired_user_ids = {
                str(user_id).strip()
                for batch in self._state["refill_batches"].values()
                for user_id in batch.get("retired_user_ids") or []
                if str(user_id).strip()
            }
            if retired_user_ids:
                self._state["available_backup_user_ids"] = [
                    user_id
                    for user_id in self._state["available_backup_user_ids"]
                    if str(user_id).strip() not in retired_user_ids
                ]
            self._normalize_available_backup_user_ids_locked()
            self._persist_state_locked()

    def _normalize_batch(self, batch_id: str, batch: dict[str, Any]) -> dict[str, Any]:
        retry_after_epoch = batch.get("retry_after_epoch")
        try:
            normalized_retry_after_epoch = (
                float(retry_after_epoch) if retry_after_epoch is not None else None
            )
        except (TypeError, ValueError):
            normalized_retry_after_epoch = None
        normalized = {
            "batch_id": batch_id,
            "status": str(batch.get("status") or "queued"),
            "proxy_label": batch.get("proxy_label"),
            "reason": str(batch.get("reason") or "replacement"),
            "requested_count": int(batch.get("requested_count") or 0),
            "retired_user_ids": self._sanitize_user_id_list(
                batch.get("retired_user_ids")
                or batch.get("banned_user_ids")
                or [],
            ),
            "delete_complete": bool(batch.get("delete_complete", False)),
            "template_rows": list(batch.get("template_rows") or []),
            "created_profiles": list(batch.get("created_profiles") or []),
            "prepared_user_ids": list(batch.get("prepared_user_ids") or []),
            "failed_profiles": list(batch.get("failed_profiles") or []),
            "created_at_utc": batch.get("created_at_utc") or _utcnow_iso(),
            "updated_at_utc": batch.get("updated_at_utc") or _utcnow_iso(),
            "blocked_reason": batch.get("blocked_reason"),
            "error": batch.get("error"),
            "retry_attempts": int(batch.get("retry_attempts") or 0),
            "retry_after_epoch": normalized_retry_after_epoch,
        }
        if normalized["status"] in {"building_cookies", "assigning_proxy"}:
            normalized["status"] = "creating"
        for profile in normalized["created_profiles"]:
            if profile.get("prep_status") == "in_progress":
                profile["prep_status"] = "created"
        normalized["requested_count"] = max(
            int(normalized.get("requested_count") or 0),
            len(normalized["retired_user_ids"]),
        )
        return normalized

    def _persist_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "available_backup_user_ids": list(self._state["available_backup_user_ids"]),
            "template_cursor": int(self._state.get("template_cursor") or 0),
            "refill_batches": self._state["refill_batches"],
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _normalize_available_backup_user_ids_locked(self) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_user_id in self._state["available_backup_user_ids"]:
            user_id = str(raw_user_id).strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            normalized.append(user_id)
        self._state["available_backup_user_ids"] = normalized

    def _all_known_profile_ids_locked(self) -> set[str]:
        known = set(self._state["available_backup_user_ids"])
        for batch in self._state["refill_batches"].values():
            known.update(str(item).strip() for item in batch.get("retired_user_ids") or [])
            known.update(str(item).strip() for item in batch.get("prepared_user_ids") or [])
            for profile in batch.get("created_profiles") or []:
                user_id = str(profile.get("user_id") or "").strip()
                if user_id:
                    known.add(user_id)
        return {user_id for user_id in known if user_id}

    def _template_rows_loaded(self) -> list[dict[str, Any]]:
        if self._template_rows is None:
            template_source = (self.config.adspower_profile_template_source or "").strip()
            if not template_source:
                raise BackupRefillBlockedError("BOT_ADSPOWER_PROFILE_TEMPLATE_SOURCE is not configured")
            self._template_rows = load_profile_template_rows(template_source)
        return self._template_rows

    def _reserve_template_payloads_locked(self, count: int) -> list[dict[str, Any]]:
        template_rows = self._template_rows_loaded()
        if not template_rows:
            raise BackupRefillBlockedError("No template rows are available for backup refill")
        start_index = int(self._state.get("template_cursor") or 0)
        payloads: list[dict[str, Any]] = []
        for offset in range(count):
            source_row = template_rows[(start_index + offset) % len(template_rows)]
            payload = _row_to_profile_payload(source_row)
            if not payload:
                raise BackupRefillBlockedError("A reserved template row expanded to an empty AdsPower payload")
            payloads.append(payload)
        self._state["template_cursor"] = start_index + count
        self._persist_state_locked()
        return payloads

    def _resolved_extension_category_id(self) -> str | None:
        if self._extension_category_id is not None:
            return self._extension_category_id

        resolved = self.client.resolve_extension_category_id(
            category_id=self.config.adspower_extension_category_id,
            category_name=self.config.adspower_extension_category_name,
        )
        self._extension_category_id = str(resolved).strip() or None
        return self._extension_category_id

    def _apply_profile_creation_overrides(self, payload: dict[str, Any]) -> dict[str, Any]:
        updated_payload = dict(payload)
        existing_extension_category_id = str(updated_payload.get("sys_app_cate_id") or "").strip()
        if existing_extension_category_id:
            return updated_payload
        extension_category_id = self._resolved_extension_category_id()
        if extension_category_id:
            updated_payload["sys_app_cate_id"] = extension_category_id
        return updated_payload

    def _next_batch_id(self) -> str | None:
        now = time.time()
        with self._lock:
            for batch_id, batch in self._state["refill_batches"].items():
                if str(batch.get("status") or "") in {"queued", "deleting", "creating"}:
                    retry_after_epoch = batch.get("retry_after_epoch")
                    if retry_after_epoch is not None and float(retry_after_epoch) > now:
                        continue
                    return batch_id
        return None

    def _run(self) -> None:
        while not self.stop_event.is_set():
            batch_id = self._next_batch_id()
            if not batch_id:
                time.sleep(0.5)
                continue
            try:
                self._process_batch(batch_id)
            except Exception as error:  # noqa: BLE001
                self._mark_batch_failed(batch_id, str(error))
                time.sleep(1.0)

    def _mark_batch_failed(self, batch_id: str, error: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            batch["status"] = "failed"
            batch["error"] = error
            batch["retry_attempts"] = 0
            batch["retry_after_epoch"] = None
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()
        print(f"Dynamic backup refill batch {batch_id} failed: {error}")

    def _mark_batch_blocked(self, batch_id: str, reason: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            batch["status"] = "blocked"
            batch["blocked_reason"] = reason
            batch["retry_attempts"] = 0
            batch["retry_after_epoch"] = None
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()
        print(f"Dynamic backup refill batch {batch_id} blocked: {reason}")

    def _schedule_batch_retry(
        self,
        batch_id: str,
        error: str,
        *,
        base_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            retry_attempts = int(batch.get("retry_attempts") or 0) + 1
            delay_seconds = min(
                max_delay_seconds,
                base_delay_seconds * (2 ** max(0, retry_attempts - 1)),
            )
            batch["status"] = "queued"
            batch["error"] = error
            batch["retry_attempts"] = retry_attempts
            batch["retry_after_epoch"] = time.time() + delay_seconds
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()
        print(
            f"Dynamic backup refill batch {batch_id} will retry in {delay_seconds:.1f}s: {error}"
        )

    def _clear_batch_retry_state_locked(self, batch: dict[str, Any]) -> None:
        batch["retry_attempts"] = 0
        batch["retry_after_epoch"] = None

    def _ensure_profiles_stopped_before_delete(self, user_ids: list[str]) -> None:
        pending_user_ids = [str(user_id).strip() for user_id in user_ids if str(user_id).strip()]
        if not pending_user_ids:
            return

        for user_id in pending_user_ids:
            try:
                self.client.stop_browser(user_id)
            except Exception as error:  # noqa: BLE001
                print(f"Dynamic backup refill: failed to stop browser for {user_id} before delete: {error}")

        deadline = time.monotonic() + max(3.0, 0.75 * len(pending_user_ids))
        while pending_user_ids and time.monotonic() < deadline:
            still_active: list[str] = []
            for user_id in pending_user_ids:
                try:
                    if self.client.is_browser_active(user_id):
                        still_active.append(user_id)
                except AdsPowerCapabilityError:
                    return
                except Exception as error:  # noqa: BLE001
                    if _is_adspower_rate_limit_error(error):
                        time.sleep(1.0)
                        continue
                    print(
                        f"Dynamic backup refill: failed to query browser state for {user_id} before delete: {error}"
                    )
                    still_active.append(user_id)
            if not still_active:
                return
            for user_id in still_active:
                try:
                    self.client.stop_browser(user_id)
                except Exception as error:  # noqa: BLE001
                    print(
                        f"Dynamic backup refill: repeat stop failed for {user_id} before delete: {error}"
                    )
            pending_user_ids = still_active
            time.sleep(0.5)

        if pending_user_ids:
            print(
                "Dynamic backup refill: proceeding with delete while profiles still appear active: "
                f"{pending_user_ids}"
            )

    def _process_batch(self, batch_id: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return

        if not bool(batch.get("delete_complete")):
            with self._lock:
                batch = self._state["refill_batches"].get(batch_id)
                if batch is None:
                    return
                batch["status"] = "deleting"
                batch["updated_at_utc"] = _utcnow_iso()
                self._persist_state_locked()
                retired_user_ids = list(batch.get("retired_user_ids") or [])
            try:
                self._ensure_profiles_stopped_before_delete(retired_user_ids)
                self.client.delete_profiles(retired_user_ids)
            except AdsPowerCapabilityError as error:
                if self.config.backup_refill_block_on_create_delete_unsupported:
                    self._mark_batch_blocked(batch_id, str(error))
                    return
                self._mark_batch_failed(batch_id, str(error))
                return
            except Exception as error:  # noqa: BLE001
                if _is_adspower_empty_delete_in_use_error(error):
                    with self._lock:
                        batch = self._state["refill_batches"].get(batch_id)
                        if batch is None:
                            return
                        batch["delete_complete"] = True
                        batch["status"] = "creating"
                        batch["error"] = (
                            "Delete returned an empty in-use list; "
                            "treating retired profiles as already absent"
                        )
                        self._clear_batch_retry_state_locked(batch)
                        batch["updated_at_utc"] = _utcnow_iso()
                        self._persist_state_locked()
                    print(
                        f"Dynamic backup refill batch {batch_id}: delete reported an empty in-use list; "
                        "treating retired profiles as already deleted"
                    )
                    return
                if (
                    _is_adspower_rate_limit_error(error)
                    or _is_adspower_delete_in_use_error(error)
                ):
                    self._schedule_batch_retry(
                        batch_id,
                        f"delete retry needed: {error}",
                        base_delay_seconds=15.0,
                        max_delay_seconds=300.0,
                    )
                    return
                self._mark_batch_failed(batch_id, f"delete failed: {error}")
                return
            with self._lock:
                batch = self._state["refill_batches"].get(batch_id)
                if batch is None:
                    return
                batch["delete_complete"] = True
                batch["status"] = "creating"
                batch["error"] = None
                self._clear_batch_retry_state_locked(batch)
                batch["updated_at_utc"] = _utcnow_iso()
                self._persist_state_locked()

        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            template_rows = list(batch.get("template_rows") or [])
            if not template_rows:
                try:
                    template_rows = self._reserve_template_payloads_locked(
                        int(batch.get("requested_count") or 0)
                    )
                except BackupRefillBlockedError as error:
                    self._mark_batch_blocked(batch_id, str(error))
                    return
                except Exception as error:  # noqa: BLE001
                    self._mark_batch_failed(batch_id, f"template reservation failed: {error}")
                    return
                batch["template_rows"] = template_rows
                batch["updated_at_utc"] = _utcnow_iso()
                self._persist_state_locked()

        self._create_profiles_for_batch(batch_id)
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None or str(batch.get("status") or "") in {"failed", "blocked"}:
                return
        self._prepare_profiles_for_batch(batch_id)

    def _create_profiles_for_batch(self, batch_id: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            batch["status"] = "creating"
            batch["updated_at_utc"] = _utcnow_iso()
            template_rows = list(batch.get("template_rows") or [])
            created_profiles = list(batch.get("created_profiles") or [])
            self._persist_state_locked()

        for row_index in range(len(created_profiles), len(template_rows)):
            payload = self._apply_profile_creation_overrides(template_rows[row_index])
            try:
                profile_id = self.client.create_profile(payload)
            except AdsPowerCapabilityError as error:
                if self.config.backup_refill_block_on_create_delete_unsupported:
                    self._mark_batch_blocked(batch_id, str(error))
                    return
                self._mark_batch_failed(batch_id, str(error))
                return
            except Exception as error:  # noqa: BLE001
                if _is_adspower_profile_quota_error(error):
                    if row_index > 0:
                        with self._lock:
                            batch = self._state["refill_batches"].get(batch_id)
                            if batch is None:
                                return
                            created_count = len(batch.get("created_profiles") or [])
                            batch["requested_count"] = created_count
                            batch["template_rows"] = list(batch.get("template_rows") or [])[:created_count]
                            batch["error"] = (
                                "AdsPower profile quota was reached mid-batch; "
                                f"continuing with {created_count} created profile(s)"
                            )
                            self._clear_batch_retry_state_locked(batch)
                            batch["updated_at_utc"] = _utcnow_iso()
                            self._persist_state_locked()
                        print(
                            f"Dynamic backup refill batch {batch_id}: AdsPower profile quota was reached "
                            f"after creating {row_index}/{len(template_rows)} profiles; "
                            "continuing with the partial batch"
                        )
                        return
                    self._schedule_batch_retry(
                        batch_id,
                        f"profile creation paused because AdsPower profile quota is full: {error}",
                        base_delay_seconds=max(30.0, float(self.config.restart_backoff_seconds)),
                        max_delay_seconds=600.0,
                    )
                    return
                if _is_adspower_rate_limit_error(error):
                    self._schedule_batch_retry(
                        batch_id,
                        f"profile creation rate-limited: {error}",
                        base_delay_seconds=15.0,
                        max_delay_seconds=180.0,
                    )
                    return
                self._mark_batch_failed(batch_id, f"profile creation failed: {error}")
                return
            with self._lock:
                batch = self._state["refill_batches"].get(batch_id)
                if batch is None:
                    return
                created_profiles = list(batch.get("created_profiles") or [])
                created_profiles.append(
                    {
                        "user_id": profile_id,
                        "prep_status": "created",
                        "error": None,
                        "creation_proxyid": payload.get("proxyid"),
                    }
                )
                batch["created_profiles"] = created_profiles
                self._clear_batch_retry_state_locked(batch)
                batch["updated_at_utc"] = _utcnow_iso()
                self._persist_state_locked()
            print(
                f"Dynamic backup refill batch {batch_id}: created profile {profile_id} "
                f"({row_index + 1}/{len(template_rows)})"
            )

    def _prepare_profiles_for_batch(self, batch_id: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            created_profiles = [
                profile
                for profile in batch.get("created_profiles") or []
                if str(profile.get("prep_status") or "") == "created"
            ]
            if not created_profiles:
                self._finalize_batch_status_locked(batch_id)
                return
            batch["status"] = "building_cookies"
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()

        max_workers = max(1, int(self.config.backup_refill_prep_concurrency or 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._prepare_single_profile, batch_id, str(profile["user_id"])): str(profile["user_id"])
                for profile in created_profiles
            }
            for future in as_completed(future_map):
                user_id = future_map[future]
                try:
                    future.result()
                except BackupRefillBlockedError as error:
                    self._record_profile_failure(batch_id, user_id, str(error), blocked=True)
                    return
                except Exception as error:  # noqa: BLE001
                    self._record_profile_failure(batch_id, user_id, str(error), blocked=False)

        with self._lock:
            self._finalize_batch_status_locked(batch_id)

    def _record_profile_failure(self, batch_id: str, user_id: str, error: str, *, blocked: bool) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            created_profiles = list(batch.get("created_profiles") or [])
            for profile in created_profiles:
                if str(profile.get("user_id") or "") == user_id:
                    profile["prep_status"] = "failed"
                    profile["error"] = error
            failed_profiles = list(batch.get("failed_profiles") or [])
            failed_profiles.append({"user_id": user_id, "error": error})
            batch["created_profiles"] = created_profiles
            batch["failed_profiles"] = failed_profiles
            batch["updated_at_utc"] = _utcnow_iso()
            if blocked:
                batch["status"] = "blocked"
                batch["blocked_reason"] = error
            self._persist_state_locked()
        if blocked:
            print(f"Dynamic backup refill batch {batch_id}: blocked while preparing {user_id}: {error}")
        else:
            print(f"Dynamic backup refill batch {batch_id}: preparation failed for {user_id}: {error}")

    def _prepare_single_profile(self, batch_id: str, user_id: str) -> None:
        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            created_profiles = list(batch.get("created_profiles") or [])
            for profile in created_profiles:
                if str(profile.get("user_id") or "") == user_id:
                    profile["prep_status"] = "in_progress"
            batch["status"] = "building_cookies"
            batch["created_profiles"] = created_profiles
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()

        self._build_cookies_for_profile(batch_id, user_id)

        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            batch["status"] = "assigning_proxy"
            batch["updated_at_utc"] = _utcnow_iso()
            creation_proxyid = None
            for profile in batch.get("created_profiles") or []:
                if str(profile.get("user_id") or "") == user_id:
                    creation_proxyid = profile.get("creation_proxyid")
                    break
            self._persist_state_locked()

        self._assign_proxy_to_profile(user_id, creation_proxyid=creation_proxyid)

        with self._lock:
            batch = self._state["refill_batches"].get(batch_id)
            if batch is None:
                return
            created_profiles = list(batch.get("created_profiles") or [])
            for profile in created_profiles:
                if str(profile.get("user_id") or "") == user_id:
                    profile["prep_status"] = "ready"
                    profile["error"] = None
            batch["created_profiles"] = created_profiles
            batch["updated_at_utc"] = _utcnow_iso()
            self._persist_state_locked()

        self.append_ready_backup_user_id(user_id, batch_id=batch_id)
        print(f"Dynamic backup refill batch {batch_id}: profile {user_id} is backup-ready")

    def _build_cookies_for_profile(self, batch_id: str, user_id: str) -> None:
        if not self.config.backup_refill_cookie_build_enabled:
            print(
                f"Dynamic backup refill batch {batch_id}: skipping cookie build for {user_id} "
                "because BOT_BACKUP_REFILL_COOKIE_BUILD_ENABLED=false"
            )
            return
        job_id = (self.config.adspower_cookie_rpa_job_id or "").strip()
        if job_id:
            try:
                job_run_id = self.client.trigger_cookie_rpa_job(user_id, job_id)
                status = self.client.poll_cookie_rpa_job(job_run_id)
                if str(status.get("status") or "").lower() not in {"success", "completed", "done"}:
                    raise RuntimeError(
                        f"AdsPower cookie RPA returned non-success status for {user_id}: {status}"
                    )
                self._wait_for_profile_close_after_cookie_build(user_id)
                return
            except AdsPowerCapabilityError:
                self._run_cookie_rpa_commands(batch_id, user_id, job_id)
                return

        command_template = (self.config.backup_refill_cookie_build_command or "").strip()
        if not command_template:
            raise BackupRefillBlockedError(
                "No AdsPower RPA trigger support is configured and "
                "BOT_BACKUP_REFILL_COOKIE_BUILD_COMMAND is empty"
            )
        command = command_template.format(
            user_id=user_id,
            profile_id=user_id,
            batch_id=batch_id,
        )
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(self.config.backup_refill_cookie_build_timeout_seconds)),
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(f"cookie build command failed for {user_id}: {detail}")

    def _run_cookie_rpa_commands(self, batch_id: str, user_id: str, job_id: str) -> None:
        launch_template = (self.config.adspower_cookie_rpa_launch_command or "").strip()
        if not launch_template:
            raise BackupRefillBlockedError(
                "BOT_ADSPOWER_COOKIE_RPA_JOB_ID is set but BOT_ADSPOWER_COOKIE_RPA_LAUNCH_COMMAND is empty"
            )
        launch_command = launch_template.format(
            user_id=user_id,
            profile_id=user_id,
            batch_id=batch_id,
            job_id=job_id,
        )
        completed = subprocess.run(
            launch_command,
            shell=True,
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(self.config.backup_refill_cookie_build_timeout_seconds)),
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(f"cookie RPA launch command failed for {user_id}: {detail}")

        job_run_id = (completed.stdout or "").strip()
        status_template = (self.config.adspower_cookie_rpa_status_command or "").strip()
        if status_template:
            self._poll_cookie_rpa_status_command(
                batch_id=batch_id,
                user_id=user_id,
                job_id=job_id,
                job_run_id=job_run_id,
                status_template=status_template,
            )
        self._wait_for_profile_close_after_cookie_build(user_id)

    def _poll_cookie_rpa_status_command(
        self,
        *,
        batch_id: str,
        user_id: str,
        job_id: str,
        job_run_id: str,
        status_template: str,
    ) -> None:
        deadline = time.monotonic() + max(
            1.0,
            float(self.config.backup_refill_cookie_build_timeout_seconds),
        )
        poll_seconds = max(0.5, float(self.config.adspower_cookie_rpa_poll_seconds))
        while time.monotonic() < deadline:
            command = status_template.format(
                user_id=user_id,
                profile_id=user_id,
                batch_id=batch_id,
                job_id=job_id,
                job_run_id=job_run_id,
            )
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                timeout=max(1.0, poll_seconds),
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = stderr or stdout or f"exit code {completed.returncode}"
                raise RuntimeError(f"cookie RPA status command failed for {user_id}: {detail}")
            status_payload = _parse_rpa_status_payload((completed.stdout or "").strip())
            status = str(status_payload.get("status") or "").lower()
            if status in {"success", "completed", "done", "ok", "closed"}:
                return
            if status in {"failed", "error", "timeout", "cancelled"}:
                raise RuntimeError(
                    f"cookie RPA reported terminal failure for {user_id}: {status_payload}"
                )
            time.sleep(poll_seconds)
        raise RuntimeError(f"cookie RPA status polling timed out for {user_id}")

    def _wait_for_profile_close_after_cookie_build(self, user_id: str) -> None:
        if not self.config.adspower_cookie_rpa_require_browser_close:
            return
        deadline = time.monotonic() + max(
            1.0,
            float(self.config.backup_refill_cookie_build_timeout_seconds),
        )
        poll_seconds = max(0.5, float(self.config.adspower_cookie_rpa_poll_seconds))
        while time.monotonic() < deadline:
            try:
                if not self.client.is_browser_active(user_id):
                    return
            except AdsPowerCapabilityError:
                return
            time.sleep(poll_seconds)
        raise RuntimeError(
            f"cookie RPA finished but AdsPower profile {user_id} never closed within the configured timeout"
        )

    def _saved_proxy_candidates(self, source: str) -> list[str]:
        cached = self._proxy_query_cache.get(source)
        if cached is not None:
            return list(cached)
        candidates: list[str] = []
        if source.lower().startswith("tag:"):
            target_tag = source.split(":", 1)[1].strip().lower()
            page = 1
            while True:
                rows, _total = self.client.list_saved_proxies(page=page, limit=200)
                if not rows:
                    break
                for row in rows:
                    proxy_id = str(row.get("proxy_id") or row.get("id") or "").strip()
                    raw_tags = row.get("tag") or row.get("tags") or []
                    if isinstance(raw_tags, str):
                        tags = [item.strip().lower() for item in raw_tags.split(",") if item.strip()]
                    else:
                        tags = [str(item).strip().lower() for item in raw_tags if str(item).strip()]
                    if proxy_id and target_tag in tags:
                        candidates.append(proxy_id)
                if len(rows) < 200:
                    break
                page += 1
        else:
            raw_ids = source.split(":", 1)[1] if source.lower().startswith("ids:") else source
            candidates = [item.strip() for item in raw_ids.split(",") if item.strip()]
        self._proxy_query_cache[source] = list(candidates)
        return candidates

    def _assign_proxy_to_profile(self, user_id: str, *, creation_proxyid: Any = None) -> None:
        source = (self.config.backup_refill_proxy_source or "random").strip()
        if (
            source.lower() in {"random", "saved_random", "random_saved"}
            and str(creation_proxyid or "").strip().lower() == "random"
        ):
            return
        if not source or source.lower() in {"random", "saved_random", "random_saved"}:
            desired_proxy_id = "random"
        else:
            candidates = self._saved_proxy_candidates(source)
            if not candidates:
                raise BackupRefillBlockedError(
                    f"No saved proxies matched BOT_BACKUP_REFILL_PROXY_SOURCE={source!r}"
                )
            desired_proxy_id = random.choice(candidates)

        retry_delays = (0.75, 1.5, 2.5)
        last_error: Exception | None = None
        for attempt_index in range(len(retry_delays) + 1):
            try:
                self.client.update_profile_saved_proxy(user_id, desired_proxy_id)
                return
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not _is_adspower_rate_limit_error(error) or attempt_index >= len(retry_delays):
                    break
                time.sleep(retry_delays[attempt_index])
        assert last_error is not None
        raise last_error

    def _finalize_batch_status_locked(self, batch_id: str) -> None:
        batch = self._state["refill_batches"].get(batch_id)
        if batch is None:
            return
        created_profiles = list(batch.get("created_profiles") or [])
        if not created_profiles:
            batch["status"] = "failed"
            batch["error"] = "No profiles were created for this refill batch"
        elif all(str(profile.get("prep_status") or "") == "ready" for profile in created_profiles):
            batch["status"] = "ready"
            batch["error"] = None
        elif any(str(profile.get("prep_status") or "") == "ready" for profile in created_profiles):
            batch["status"] = "failed"
            batch["error"] = "One or more profiles in the refill batch failed during preparation"
        elif str(batch.get("status") or "") != "blocked":
            batch["status"] = "failed"
            batch["error"] = batch.get("error") or "All created profiles failed during preparation"
        batch["retry_attempts"] = 0
        batch["retry_after_epoch"] = None
        batch["updated_at_utc"] = _utcnow_iso()
        self._persist_state_locked()
