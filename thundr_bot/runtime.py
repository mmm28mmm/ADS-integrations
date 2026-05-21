from __future__ import annotations

import queue
import json
import logging
import multiprocessing
import random
import signal
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from thundr_bot.adspower_client import AdsPowerClient
from thundr_bot.backup_refill import DynamicBackupPoolManager
from thundr_bot.chat_worker import _ensure_active_thundr_tab, is_banned, log_ctx, run_session, setup_logger
from thundr_bot.config import BotConfig, RuntimeContext, SessionResult
from thundr_bot.registration_worker import run_registration


def _is_interrupt_error(error: BaseException) -> bool:
    if isinstance(error, (KeyboardInterrupt, InterruptedError)):
        return True
    if isinstance(error, OSError) and getattr(error, "errno", None) == 4:
        return True
    return False


def _install_signal_handlers(stop_event: multiprocessing.synchronize.Event) -> None:
    def _handler(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _proxy_label_from_index(index: int) -> str:
    if index < 26:
        return chr(ord("A") + index)
    return f"P{index + 1}"


def _resolve_texting_proxy_pool(config: BotConfig) -> list[str]:
    if config.texting_proxy_http_list:
        return [value.strip() for value in config.texting_proxy_http_list if value.strip()]
    proxy_value = (config.texting_proxy_http or "").strip()
    return [proxy_value] if proxy_value else []


def _assigned_slot_proxy(
    slot_id: int,
    proxy_pool: list[str],
    max_instances_per_proxy: int,
) -> tuple[str, str] | None:
    if not proxy_pool:
        return None
    if max_instances_per_proxy <= 0:
        raise ValueError("BOT_TEXTING_PROXY_MAX_INSTANCES_PER_PROXY must be greater than 0")
    proxy_index = slot_id // max_instances_per_proxy
    if proxy_index >= len(proxy_pool):
        raise ValueError(
            "Configured texting proxy capacity is smaller than the requested slot count"
        )
    return proxy_pool[proxy_index], _proxy_label_from_index(proxy_index)


def _apply_texting_proxy_assignment(
    *,
    client: AdsPowerClient,
    logger: logging.Logger,
    ctx: RuntimeContext,
    user_id: str,
    proxy_value: str | None,
    proxy_label: str | None,
    reason: str,
) -> tuple[bool, str | None]:
    if not proxy_value:
        return False, "TEXTING_PROXY_NOT_CONFIGURED"
    try:
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"{reason}; assigning texting proxy {proxy_label or '<unlabeled>'}",
        )
        client.update_profile_http_proxy(user_id, proxy_value)
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"Texting proxy replacement applied successfully ({proxy_label or '<unlabeled>'})",
        )
        return True, None
    except Exception as error:  # noqa: BLE001
        log_ctx(logger, logging.ERROR, ctx, f"Failed to replace profile proxy for texting phase: {error}")
        return False, "TEXTING_PROXY_REPLACEMENT_FAILED"


def _proxy_requests_mapping(proxy_value: str) -> dict[str, str]:
    host, port, username, password = [part.strip() for part in proxy_value.split(":", 3)]
    proxy_url = f"http://{username}:{password}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _fetch_proxy_exit_ip(config: BotConfig, proxy_value: str, check_url: str) -> str:
    response = requests.get(
        check_url,
        proxies=_proxy_requests_mapping(proxy_value),
        timeout=config.api_timeout_seconds,
    )
    response.raise_for_status()
    return response.text.strip()


def _fetch_proxy_exit_ip_with_retries(config: BotConfig, proxy_value: str, check_url: str) -> str:
    attempts = max(1, config.proxy_rotation_ip_check_attempts)
    retry_delay = max(0.0, config.proxy_rotation_ip_check_retry_delay_seconds)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_proxy_exit_ip(config, proxy_value, check_url)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt >= attempts:
                break
            time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def _fetch_proxy_exit_ips_with_retries(config: BotConfig, proxy_value: str) -> dict[str, str]:
    ipv4 = _fetch_proxy_exit_ip_with_retries(
        config,
        proxy_value,
        config.proxy_rotation_ipv4_check_url,
    )
    ipv6 = _fetch_proxy_exit_ip_with_retries(
        config,
        proxy_value,
        config.proxy_rotation_ipv6_check_url,
    )
    return {"ipv4": ipv4, "ipv6": ipv6}


def _fetch_proxy_exit_ip_consensus(
    config: BotConfig,
    proxy_value: str,
    check_urls: list[str],
) -> tuple[str | None, dict[str, str], dict[str, str]]:
    responses: dict[str, str] = {}
    errors: dict[str, str] = {}
    for check_url in check_urls:
        try:
            responses[check_url] = _fetch_proxy_exit_ip_with_retries(config, proxy_value, check_url)
        except Exception as error:  # noqa: BLE001
            errors[check_url] = str(error)

    min_providers = max(1, config.proxy_rotation_ip_consensus_min_providers)
    counts = Counter(responses.values())
    consensus_value: str | None = None
    if counts:
        value, count = counts.most_common(1)[0]
        if count >= min_providers:
            consensus_value = value
    return consensus_value, responses, errors


def _fetch_proxy_exit_ip_consensus_pair(
    config: BotConfig,
    proxy_value: str,
) -> tuple[dict[str, str | None], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    ipv4_value, ipv4_responses, ipv4_errors = _fetch_proxy_exit_ip_consensus(
        config,
        proxy_value,
        config.proxy_rotation_ipv4_check_urls,
    )
    ipv6_value, ipv6_responses, ipv6_errors = _fetch_proxy_exit_ip_consensus(
        config,
        proxy_value,
        config.proxy_rotation_ipv6_check_urls,
    )
    return (
        {"ipv4": ipv4_value, "ipv6": ipv6_value},
        {"ipv4": ipv4_responses, "ipv6": ipv6_responses},
        {"ipv4": ipv4_errors, "ipv6": ipv6_errors},
    )


def _trigger_proxy_rotation_link(config: BotConfig, rotation_url: str) -> dict[str, object]:
    response = requests.get(rotation_url, timeout=config.api_timeout_seconds)
    response.raise_for_status()
    body_text = response.text.strip()
    body_snippet = body_text.replace("\n", " ")[:240]
    message: str | None = None
    acknowledged = False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_message = payload.get("message")
        if isinstance(raw_message, str):
            message = raw_message.strip()
            acknowledged = message.lower() == "changeip command has been sent"
    return {
        "status_code": response.status_code,
        "body_snippet": body_snippet,
        "message": message,
        "acknowledged": acknowledged,
    }


def _worker_main(
    slot_id: int,
    session_id: int,
    user_id: str,
    run_id: str,
    config: BotConfig,
    needs_registration: bool,
    assigned_texting_proxy: str | None,
    assigned_texting_proxy_label: str | None,
    stop_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
    probe_only: bool = False,
) -> None:
    worker_stop_event = stop_event
    ban_signal_sent = False

    def _local_signal_handler(signum, _frame):
        worker_stop_event.set()
        stop_event.set()

    def _emit_ban_signal_once() -> None:
        nonlocal ban_signal_sent
        if ban_signal_sent:
            return
        ban_signal_sent = True
        result_queue.put(
            {
                "kind": "ban_signal",
                "slot_id": slot_id,
                "session_id": session_id,
                "user_id": user_id,
                "assigned_texting_proxy_label": assigned_texting_proxy_label,
                "probe_only": probe_only,
            }
        )

    signal.signal(signal.SIGINT, _local_signal_handler)
    signal.signal(signal.SIGTERM, _local_signal_handler)

    ctx = RuntimeContext(run_id=run_id, session_id=session_id, user_id=user_id)
    logger = setup_logger(config, ctx)
    client = AdsPowerClient(config)
    registration_attempted = False
    registration_ok = None
    if needs_registration:
        registration_attempted = True
        log_ctx(logger, logging.INFO, ctx, "Registration required before chat flow")
        reg_ok = run_registration(user_id, config, logger, ctx, worker_stop_event)
        registration_ok = reg_ok
        if not reg_ok:
            result_queue.put(
                {
                    "slot_id": slot_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "status": "failed",
                    "attempts": 1,
                    "fatal_error": "REGISTRATION_FAILED",
                    "registration_attempted": registration_attempted,
                    "registration_ok": registration_ok,
                    "chatted": False,
                    "opener_sends": 0,
                    "cta_sends": 0,
                }
            )
            return
        if config.replace_proxy_for_texting:
            ok, fatal_error = _apply_texting_proxy_assignment(
                client=client,
                logger=logger,
                ctx=ctx,
                user_id=user_id,
                proxy_value=assigned_texting_proxy,
                proxy_label=assigned_texting_proxy_label,
                reason="Registration succeeded; replacing profile proxy for texting phase",
            )
            if not ok:
                result_queue.put(
                    {
                        "slot_id": slot_id,
                        "session_id": session_id,
                        "user_id": user_id,
                        "status": "failed",
                        "attempts": 1,
                        "fatal_error": fatal_error,
                        "registration_attempted": registration_attempted,
                        "registration_ok": registration_ok,
                        "assigned_texting_proxy_label": assigned_texting_proxy_label,
                        "chatted": False,
                        "opener_sends": 0,
                        "cta_sends": 0,
                    }
                )
                return
    elif config.replace_proxy_for_texting and config.enforce_texting_proxy_on_chat_start:
        ok, fatal_error = _apply_texting_proxy_assignment(
            client=client,
            logger=logger,
            ctx=ctx,
            user_id=user_id,
            proxy_value=assigned_texting_proxy,
            proxy_label=assigned_texting_proxy_label,
            reason="Applying slot-assigned texting proxy before chat start",
        )
        if not ok:
            result_queue.put(
                {
                    "slot_id": slot_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "status": "failed",
                    "attempts": 1,
                    "fatal_error": fatal_error,
                    "registration_attempted": registration_attempted,
                    "registration_ok": registration_ok,
                    "assigned_texting_proxy_label": assigned_texting_proxy_label,
                    "chatted": False,
                    "opener_sends": 0,
                    "cta_sends": 0,
                }
            )
            return

    result = run_session(
        user_id,
        config,
        logger,
        ctx,
        worker_stop_event,
        startup_probe_only=probe_only,
        on_ban_detected=_emit_ban_signal_once,
    )

    if result.status in {"success", "stopped"}:
        level = logging.INFO
    elif result.status == "parked":
        level = logging.WARNING
    else:
        level = logging.ERROR
    log_ctx(
        logger,
        level,
        ctx,
        f"Session result: status={result.status} attempts={result.attempts} error={result.fatal_error}",
    )
    result_queue.put(
        {
            "slot_id": slot_id,
            "session_id": session_id,
            "user_id": user_id,
            "status": result.status,
            "attempts": result.attempts,
            "fatal_error": result.fatal_error,
            "registration_attempted": registration_attempted,
            "registration_ok": registration_ok,
            "assigned_texting_proxy_label": assigned_texting_proxy_label,
            "chatted": result.chatted,
            "opener_sends": result.opener_sends,
            "cta_sends": result.cta_sends,
            "probe_only": probe_only,
        }
    )


class RuntimeController:
    def __init__(self, config: BotConfig):
        self.config = config
        self.run_id = uuid.uuid4().hex[:12]
        self.stop_event = multiprocessing.Event()
        self.summary: dict[str, dict] = {}
        self.summary_path: Path | None = None
        self.final_slot_user_ids_csv: str = ""
        self.remaining_backup_user_ids_csv: str = ""
        self.adspower_client = AdsPowerClient(config)
        self.backup_pool_manager: DynamicBackupPoolManager | None = None
        try:
            self.remark_timezone = ZoneInfo("Europe/Sofia")
        except ZoneInfoNotFoundError:
            # Windows Python may not have IANA tz data installed.
            self.remark_timezone = timezone(timedelta(hours=2))

    def _mark_started(
        self,
        user_id: str,
        slot_id: int,
        is_backup: bool,
        assigned_texting_proxy_label: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record = self.summary.setdefault(
            user_id,
            {
                "user_id": user_id,
                "first_started_at_utc": now,
                "last_started_at_utc": now,
                "slot_id": slot_id,
                "is_backup": is_backup,
                "assigned_texting_proxy_label": assigned_texting_proxy_label,
                "registration_attempted": False,
                "registration_ok": None,
                "status": "running",
                "attempts": 0,
                "fatal_error": None,
                "replaced_by": None,
                "replaced_from": None,
                "ended_at_utc": None,
                "opener_sends": 0,
                "cta_sends": 0,
            },
        )
        record["last_started_at_utc"] = now
        record["slot_id"] = slot_id
        record["is_backup"] = is_backup
        record["assigned_texting_proxy_label"] = assigned_texting_proxy_label
        record["status"] = "running"

    def _mark_replaced(self, old_user_id: str, new_user_id: str) -> None:
        if old_user_id in self.summary:
            self.summary[old_user_id]["replaced_by"] = new_user_id
        if new_user_id in self.summary:
            self.summary[new_user_id]["replaced_from"] = old_user_id

    def _mark_result(self, result: dict) -> None:
        user_id = result["user_id"]
        record = self.summary.setdefault(
            user_id,
            {
                "user_id": user_id,
                "first_started_at_utc": datetime.now(timezone.utc).isoformat(),
                "last_started_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
        record["status"] = result.get("status")
        record["attempts"] = result.get("attempts")
        record["fatal_error"] = result.get("fatal_error")
        record["registration_attempted"] = result.get("registration_attempted", False)
        record["registration_ok"] = result.get("registration_ok")
        record["assigned_texting_proxy_label"] = result.get("assigned_texting_proxy_label")
        record["chatted"] = result.get("chatted", False)
        record["opener_sends"] = result.get("opener_sends", 0)
        record["cta_sends"] = result.get("cta_sends", 0)
        record["ended_at_utc"] = datetime.now(timezone.utc).isoformat()

    def _remark_label_for_result(self, result: dict) -> str | None:
        status = result.get("status")
        fatal_error = result.get("fatal_error")

        if status == "banned":
            return "BANNED"
        if fatal_error == "REGISTRATION_FAILED":
            return "REGFAIL"
        if status == "parked":
            return "PARKED"
        if status in {"failed", "max_retries"}:
            return "ERROR"
        if status == "success":
            return "RAN"
        if status == "stopped":
            return "RAN" if result.get("chatted") else "STOPPED"
        return None

    def _format_remark(self, label: str) -> str:
        timestamp = datetime.now(self.remark_timezone).strftime("%Y-%m-%d %H:%M")
        return f"{label} | {timestamp}"

    def _update_profile_remark(self, result: dict) -> None:
        label = self._remark_label_for_result(result)
        if label is None:
            return

        user_id = result["user_id"]
        remark = self._format_remark(label)
        try:
            self.adspower_client.update_profile_remark(user_id, remark)
            print(f"Updated AdsPower remark for {user_id}: {remark}")
        except Exception as error:  # noqa: BLE001
            print(f"Failed to update AdsPower remark for {user_id}: {error}")

    def _build_summary_payload(self) -> dict:
        records = list(self.summary.values())
        banned = [r["user_id"] for r in records if r.get("status") == "banned"]
        parked = [r["user_id"] for r in records if r.get("status") == "parked"]
        reg_failed = [r["user_id"] for r in records if r.get("fatal_error") == "REGISTRATION_FAILED"]
        reg_success = [
            r["user_id"]
            for r in records
            if r.get("registration_attempted") and r.get("registration_ok") is True
        ]
        total_opener_sends = sum(int(r.get("opener_sends", 0) or 0) for r in records)
        total_cta_sends = sum(int(r.get("cta_sends", 0) or 0) for r in records)
        analytics_label = "total_opener_sends" if self.config.single_message_mode else "total_cta_sends"
        analytics_value = total_opener_sends if self.config.single_message_mode else total_cta_sends
        chatted = [
            r["user_id"]
            for r in records
            if r.get("status") in {"stopped", "success", "banned", "max_retries", "parked"}
        ]
        eligible_reuse = [
            r["user_id"]
            for r in records
            if r.get("status") in {"stopped", "success"}
            and r.get("fatal_error") in (None, "")
            and r.get("status") != "banned"
            and (not r.get("registration_attempted") or r.get("registration_ok") is True)
        ]
        return {
            "run_id": self.run_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "profiles_seen": len(records),
                "banned": len(banned),
                "parked": len(parked),
                "registration_failed": len(reg_failed),
                "registered_successfully": len(reg_success),
                "eligible_for_reuse": len(eligible_reuse),
                "total_opener_sends": total_opener_sends,
                "total_cta_sends": total_cta_sends,
            },
            "analytics": {
                "displayed_message_metric_label": analytics_label,
                "displayed_message_metric_value": analytics_value,
                "env_ads_power_user_ids_csv": self.final_slot_user_ids_csv,
                "env_ads_power_backup_user_ids_csv": self.remaining_backup_user_ids_csv,
            },
            "backup_pool": (
                self.backup_pool_manager.summary_payload()
                if self.backup_pool_manager is not None
                else None
            ),
            "categories": {
                "banned_accounts": banned,
                "parked_accounts": parked,
                "registration_failed_accounts": reg_failed,
                "registered_successfully_accounts": reg_success,
                "chatted_accounts": chatted,
                "eligible_for_reuse_accounts": eligible_reuse,
            },
            "records": records,
        }

    def _write_summary(self) -> None:
        payload = self._build_summary_payload()
        log_dir = Path(self.config.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.summary_path = log_dir / f"run_summary_{self.run_id}.json"
        try:
            self.summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(
                "Run summary:",
                f"eligible={payload['totals']['eligible_for_reuse']}",
                f"banned={payload['totals']['banned']}",
                f"parked={payload['totals']['parked']}",
                f"reg_failed={payload['totals']['registration_failed']}",
                f"{payload['analytics']['displayed_message_metric_label']}={payload['analytics']['displayed_message_metric_value']}",
                (
                    f"live_backups={payload['backup_pool']['available_backup_count']}"
                    if payload.get("backup_pool")
                    else "live_backups=<disabled>"
                ),
                f"file={self.summary_path}",
            )
            print(f"ADSPOWER_USER_IDS={self.final_slot_user_ids_csv}")
            print(f"ADSPOWER_BACKUP_USER_IDS={self.remaining_backup_user_ids_csv}")
        except Exception as error:  # noqa: BLE001
            print(f"Failed to write run summary to {self.summary_path}: {error}")

    def start(self) -> SessionResult:
        multiprocessing.freeze_support()

        if not self.config.user_ids:
            return SessionResult(status="failed", attempts=0, fatal_error="No user_ids configured")

        _install_signal_handlers(self.stop_event)

        result_queue: multiprocessing.Queue = multiprocessing.Queue()
        workers: dict[int, multiprocessing.Process] = {}
        worker_user_ids: dict[int, str] = {}
        slot_current_user_ids: dict[int, str] = {}
        slot_probe_modes: dict[int, str] = {}
        completed_slots: set[int] = set()
        pending_slot_resumes: dict[int, dict[str, object]] = {}
        pending_backup_launches: dict[int, dict[str, object]] = {}
        proxy_pool = _resolve_texting_proxy_pool(self.config)
        slot_proxy_labels: dict[int, str | None] = {}
        slot_proxy_values: dict[int, str | None] = {}
        slot_started_after_rotation_resume: dict[int, bool] = {}
        slot_dead_worker_counts: dict[int, int] = {}
        proxy_group_recoveries: dict[str, dict[str, object]] = {}
        probe_slot_proxy_labels: dict[int, str] = {}
        proxy_label_to_rotation_link: dict[str, str] = {}
        ban_pending_slots: set[int] = set()
        ban_pending_users: set[str] = set()
        next_capacity_refill_at: float = 0.0

        if self.config.replace_proxy_for_texting and not proxy_pool:
            return SessionResult(
                status="failed",
                attempts=0,
                fatal_error="TEXTING_PROXY_NOT_CONFIGURED",
            )

        if self.config.replace_proxy_for_texting and len(proxy_pool) > 1:
            required_slots = len(self.config.user_ids)
            capacity = len(proxy_pool) * self.config.texting_proxy_max_instances_per_proxy
            if required_slots > capacity:
                return SessionResult(
                    status="failed",
                    attempts=0,
                    fatal_error=(
                        "TEXTING_PROXY_CAPACITY_EXCEEDED: "
                        f"required_slots={required_slots} capacity={capacity}"
                    ),
                )

        if self.config.proxy_rotation_enabled:
            if not self.config.replace_proxy_for_texting:
                return SessionResult(
                    status="failed",
                    attempts=0,
                    fatal_error="PROXY_ROTATION_REQUIRES_TEXTING_PROXY_REPLACEMENT",
                )
            if len(self.config.texting_proxy_rotation_links) != len(proxy_pool):
                return SessionResult(
                    status="failed",
                    attempts=0,
                    fatal_error=(
                        "PROXY_ROTATION_LINK_COUNT_MISMATCH: "
                        f"proxies={len(proxy_pool)} links={len(self.config.texting_proxy_rotation_links)}"
                    ),
                )
            proxy_label_to_rotation_link = {
                _proxy_label_from_index(index): rotation_link
                for index, rotation_link in enumerate(self.config.texting_proxy_rotation_links)
            }

        self.backup_pool_manager = DynamicBackupPoolManager(
            self.config,
            self.adspower_client,
            self.stop_event,
        )
        startup_pool_overlaps = self.backup_pool_manager.invalidate_user_ids(self.config.user_ids)
        self.backup_pool_manager.start()
        print(
            "Runtime startup:",
            f"active_slots={self.config.user_ids}",
            self.backup_pool_manager.startup_log_payload(),
        )
        if startup_pool_overlaps:
            print(
                "Runtime startup: removed active slot user IDs from backup pool:",
                startup_pool_overlaps,
            )

        def _needs_registration(user_id: str, is_backup: bool) -> bool:
            if not self.config.registration_enabled:
                return False
            if user_id in self.config.registered_user_ids:
                return False
            if user_id in self.config.unregistered_user_ids:
                return True
            if is_backup and self.config.auto_register_backups:
                return True
            return False

        def _start_worker_for_slot(
            slot_id: int,
            user_id: str,
            *,
            is_backup: bool,
            started_after_rotation_resume: bool = False,
            probe_only: bool = False,
        ) -> None:
            session_id = slot_id
            needs_registration = _needs_registration(user_id, is_backup)
            assigned_proxy = None
            assigned_proxy_label = None
            if self.config.replace_proxy_for_texting:
                assigned_proxy = _assigned_slot_proxy(
                    slot_id,
                    proxy_pool,
                    self.config.texting_proxy_max_instances_per_proxy,
                )
                if assigned_proxy is None:
                    raise RuntimeError("No texting proxy assignment available for slot")
                assigned_proxy, assigned_proxy_label = assigned_proxy

            slot_proxy_labels[slot_id] = assigned_proxy_label
            slot_proxy_values[slot_id] = assigned_proxy
            slot_started_after_rotation_resume[slot_id] = started_after_rotation_resume
            self.backup_pool_manager.invalidate_user_ids([user_id])
            ban_pending_slots.discard(slot_id)
            if not probe_only:
                self._mark_started(user_id, slot_id, is_backup, assigned_proxy_label)
            process = multiprocessing.Process(
                target=_worker_main,
                args=(
                    slot_id,
                    session_id,
                    user_id,
                    self.run_id,
                    self.config,
                    needs_registration,
                    assigned_proxy,
                    assigned_proxy_label,
                    self.stop_event,
                    result_queue,
                    probe_only,
                ),
                daemon=False,
            )
            process.start()
            workers[slot_id] = process
            worker_user_ids[slot_id] = user_id
            slot_current_user_ids[slot_id] = user_id
            if probe_only:
                slot_probe_modes[slot_id] = "canary_probe"
                if assigned_proxy_label:
                    probe_slot_proxy_labels[slot_id] = assigned_proxy_label
            else:
                slot_probe_modes.pop(slot_id, None)
                probe_slot_proxy_labels.pop(slot_id, None)
            pending_slot_resumes.pop(slot_id, None)
            pending_backup_launches.pop(slot_id, None)
            slot_dead_worker_counts[slot_id] = 0
            time.sleep(1)

        def _slot_has_recent_ban_indication(slot_id: int, user_id: str | None) -> bool:
            if slot_id in ban_pending_slots:
                return True
            if user_id and user_id in ban_pending_users:
                return True
            if not user_id:
                return False
            record = self.summary.get(user_id, {})
            return bool(
                record.get("status") == "banned"
                or str(record.get("fatal_error") or "").upper() == "BANNED"
            )

        def _mark_quarantined_slot(slot_id: int) -> None:
            user_id = worker_user_ids.get(slot_id)
            if not user_id:
                return
            if _slot_has_recent_ban_indication(slot_id, user_id):
                _mark_banned_slot(slot_id, user_id)
                return
            result = {
                "slot_id": slot_id,
                "session_id": slot_id,
                "user_id": user_id,
                "status": "stopped",
                "attempts": 1,
                "fatal_error": "PROXY_GROUP_QUARANTINED",
                "registration_attempted": False,
                "registration_ok": None,
                "assigned_texting_proxy_label": slot_proxy_labels.get(slot_id),
                "chatted": False,
                "opener_sends": 0,
                "cta_sends": 0,
            }
            self._mark_result(result)
            self._update_profile_remark(result)

        def _mark_banned_slot(slot_id: int, user_id: str) -> None:
            ban_pending_slots.add(slot_id)
            ban_pending_users.add(user_id)
            record = self.summary.get(user_id, {})
            result = {
                "slot_id": slot_id,
                "session_id": slot_id,
                "user_id": user_id,
                "status": "banned",
                "attempts": int(record.get("attempts", 1) or 1),
                "fatal_error": "BANNED",
                "registration_attempted": bool(record.get("registration_attempted", False)),
                "registration_ok": record.get("registration_ok"),
                "assigned_texting_proxy_label": slot_proxy_labels.get(slot_id),
                "chatted": bool(record.get("chatted", False)),
                "opener_sends": int(record.get("opener_sends", 0) or 0),
                "cta_sends": int(record.get("cta_sends", 0) or 0),
            }
            self._mark_result(result)
            self._update_profile_remark(result)

        def _should_ignore_stale_slot_event(slot_id: int, user_id: str) -> bool:
            record = self.summary.get(user_id) or {}
            if record.get("replaced_by"):
                return True
            current_user_id = str(slot_current_user_ids.get(slot_id) or "").strip()
            if current_user_id and current_user_id != user_id:
                return True
            active_worker_user_id = str(worker_user_ids.get(slot_id) or "").strip()
            if active_worker_user_id and active_worker_user_id != user_id:
                return True
            return False

        def _profile_shows_ban_page(user_id: str) -> bool:
            browser_started = False
            driver: webdriver.Chrome | None = None
            try:
                browser = self.adspower_client.start_browser(user_id)
                browser_started = True
                options = Options()
                options.add_experimental_option("debuggerAddress", browser.debugger_address)
                service = Service(executable_path=browser.webdriver_path)
                driver = webdriver.Chrome(service=service, options=options)
                _ensure_active_thundr_tab(driver, self.config.target_url)
                time.sleep(2)
                return is_banned(driver, self.config)
            except Exception as error:  # noqa: BLE001
                print(f"Failed to triage banned state for {user_id}: {error}")
                return False
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if browser_started:
                    try:
                        self.adspower_client.stop_browser(user_id)
                    except Exception as error:  # noqa: BLE001
                        print(f"Failed to stop triage browser for {user_id}: {error}")

        def _append_pending_backup_slot(
            pending_backup_slots: list[dict[str, object]],
            *,
            slot_id: int,
            user_id: str,
            reason: str,
        ) -> None:
            if any(int(item["slot_id"]) == slot_id for item in pending_backup_slots):
                return
            pending_backup_slots.append(
                {
                    "slot_id": slot_id,
                    "user_id": user_id,
                    "reason": reason,
                }
            )

        def _terminate_worker_process(
            slot_id: int,
            user_id: str,
            proc: multiprocessing.Process | None,
            *,
            reason: str,
        ) -> None:
            if proc is None or not proc.is_alive():
                return
            print(f"Stopping slot {slot_id} ({user_id}) immediately after {reason}")
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                print(
                    f"Slot {slot_id} ({user_id}) did not exit after terminate(); forcing process kill"
                )
                proc.kill()
                proc.join(timeout=2)

        def _request_profile_browser_stop(
            user_id: str,
            *,
            reason: str,
        ) -> None:
            try:
                self.adspower_client.stop_browser(user_id)
            except Exception as error:  # noqa: BLE001
                print(f"Failed to stop AdsPower browser for {user_id} during {reason}: {error}")

        def _is_profile_browser_active(user_id: str) -> bool | None:
            try:
                return self.adspower_client.is_browser_active(user_id)
            except Exception as error:  # noqa: BLE001
                print(f"Failed to query AdsPower browser state for {user_id}: {error}")
                return None

        def _stop_profile_browser_for_retirement(
            user_id: str,
            *,
            reason: str,
        ) -> bool:
            _request_profile_browser_stop(user_id, reason=reason)
            active = _is_profile_browser_active(user_id)
            if active is False:
                return True
            return active is None

        def _await_retired_profile_shutdown(
            proxy_label: str,
            recovery: dict[str, object],
        ) -> bool:
            pending_user_ids = [
                str(user_id).strip()
                for user_id in recovery.get("shutdown_pending_user_ids") or []
                if str(user_id).strip()
            ]
            if not pending_user_ids:
                return True

            remaining_user_ids: list[str] = []
            for user_id in pending_user_ids:
                active = _is_profile_browser_active(user_id)
                if active is False:
                    continue
                if active is None:
                    continue
                _request_profile_browser_stop(
                    user_id,
                    reason=f"burn-all shutdown verification for proxy group {proxy_label}",
                )
                time.sleep(0.5)
                active = _is_profile_browser_active(user_id)
                if active is False:
                    continue
                remaining_user_ids.append(user_id)

            recovery["shutdown_pending_user_ids"] = remaining_user_ids
            if not remaining_user_ids:
                print(
                    f"Proxy group {proxy_label} confirmed all retired profile browsers are closed; "
                    "proceeding with rotation"
                )
                return True

            retry_delay = 2.0
            recovery["next_retry_at"] = max(
                float(recovery.get("next_retry_at") or 0.0),
                time.monotonic() + retry_delay,
            )
            print(
                f"Proxy group {proxy_label} is waiting for {len(remaining_user_ids)} retired "
                f"profile browsers to close before rotation: {remaining_user_ids}"
            )
            return False

        def _quarantine_proxy_group(
            proxy_label: str,
            banned_slot_id: int,
            *,
            mark_all_as_banned: bool = False,
        ) -> list[dict[str, object]]:
            same_group_slots = [
                slot
                for slot, label in slot_proxy_labels.items()
                if label == proxy_label and slot in workers and slot != banned_slot_id
            ]
            quarantined_slots: list[dict[str, object]] = []
            if same_group_slots:
                action_label = "marking banned" if mark_all_as_banned else "stopping"
                print(
                    f"Quarantining proxy group {proxy_label}; {action_label} slots={same_group_slots} after ban in slot {banned_slot_id}"
                )
            for slot in same_group_slots:
                user_id = worker_user_ids.get(slot)
                if user_id:
                    record = self.summary.get(user_id, {})
                    quarantined_slots.append(
                        {
                            "slot_id": slot,
                            "user_id": user_id,
                            "is_backup": bool(record.get("is_backup", False)),
                        }
                    )
                proc = workers.get(slot)
                if user_id:
                    _terminate_worker_process(
                        slot,
                        user_id,
                        proc,
                        reason=f"{'burn-all ' if mark_all_as_banned else ''}group quarantine",
                    )
                    browser_closed = _stop_profile_browser_for_retirement(
                        user_id,
                        reason=f"{'burn-all ' if mark_all_as_banned else ''}group quarantine",
                    )
                    if quarantined_slots:
                        quarantined_slots[-1]["browser_closed"] = browser_closed
                if user_id and mark_all_as_banned:
                    _mark_banned_slot(slot, user_id)
                else:
                    _mark_quarantined_slot(slot)
                workers.pop(slot, None)
                worker_user_ids.pop(slot, None)
            return quarantined_slots

        def _terminate_slot_worker_immediately(
            slot_id: int,
            user_id: str,
            *,
            reason: str,
            mark_banned: bool,
        ) -> bool:
            proc = workers.get(slot_id)
            _terminate_worker_process(slot_id, user_id, proc, reason=reason)
            browser_closed = _stop_profile_browser_for_retirement(user_id, reason=reason)
            workers.pop(slot_id, None)
            worker_user_ids.pop(slot_id, None)
            if mark_banned:
                _mark_banned_slot(slot_id, user_id)
            else:
                _mark_quarantined_slot(slot_id)
            return browser_closed

        def _triage_quarantined_slots_for_bans(
            proxy_label: str,
            quarantined_slots: list[dict[str, object]],
        ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
            if not quarantined_slots:
                return [], []
            print(
                f"Triage for proxy group {proxy_label}: checking {len(quarantined_slots)} quarantined slots for additional bans before rotation"
            )
            triaged_banned_slots: list[dict[str, object]] = []
            resume_candidates: list[dict[str, object]] = []
            for slot_info in quarantined_slots:
                slot_id = int(slot_info["slot_id"])
                user_id = str(slot_info["user_id"])
                is_backup = bool(slot_info["is_backup"])
                if _slot_has_recent_ban_indication(slot_id, user_id) or _profile_shows_ban_page(user_id):
                    print(
                        f"Pre-rotation triage detected additional banned profile in proxy group {proxy_label}: "
                        f"slot {slot_id} ({user_id})"
                    )
                    _mark_banned_slot(slot_id, user_id)
                    triaged_banned_slots.append(
                        {
                            "slot_id": slot_id,
                            "user_id": user_id,
                            "is_backup": is_backup,
                        }
                    )
                else:
                    resume_candidates.append(slot_info)
            return triaged_banned_slots, resume_candidates

        def _schedule_quarantined_slot_resumes(
            proxy_label: str,
            quarantined_slots: list[dict[str, object]],
        ) -> None:
            if not quarantined_slots:
                return
            now = time.monotonic()
            stagger_seconds = max(0.0, self.config.proxy_rotation_resume_stagger_seconds)
            first_resume_delay = max(0.0, self.config.proxy_rotation_first_resume_delay_seconds)
            print(
                f"Proxy group {proxy_label} rotation verified; scheduling {len(quarantined_slots)} quarantined slots for staggered resume"
            )
            for index, slot_info in enumerate(sorted(quarantined_slots, key=lambda item: int(item["slot_id"]))):
                slot_id = int(slot_info["slot_id"])
                user_id = str(slot_info["user_id"])
                is_backup = bool(slot_info["is_backup"])
                resume_delay = first_resume_delay + (index * stagger_seconds)
                resume_at = now + resume_delay
                pending_slot_resumes[slot_id] = {
                    "user_id": user_id,
                    "is_backup": is_backup,
                    "resume_at": resume_at,
                    "proxy_label": proxy_label,
                }
                print(
                    f"Scheduled slot {slot_id} ({user_id}) for proxy group {proxy_label} resume in {resume_delay:.1f}s"
                )

        def _drain_pending_slot_resumes() -> None:
            if not pending_slot_resumes:
                return
            now = time.monotonic()
            ready_slots = [
                slot_id
                for slot_id, resume_info in pending_slot_resumes.items()
                if now >= float(resume_info["resume_at"])
            ]
            for slot_id in sorted(ready_slots):
                if self.stop_event.is_set() or slot_id in workers:
                    continue
                resume_info = pending_slot_resumes.pop(slot_id, None)
                if not resume_info:
                    continue
                user_id = str(resume_info["user_id"])
                is_backup = bool(resume_info["is_backup"])
                proxy_label = str(resume_info["proxy_label"])
                print(f"Resuming slot {slot_id} on proxy group {proxy_label} with user {user_id}")
                _start_worker_for_slot(
                    slot_id,
                    user_id,
                    is_backup=is_backup,
                    started_after_rotation_resume=True,
                )
                completed_slots.discard(slot_id)

        def _backup_reservation_exclusions(*extra_user_ids: str) -> set[str]:
            excluded_user_ids: set[str] = set()
            excluded_user_ids.update(
                str(user_id).strip()
                for user_id in slot_current_user_ids.values()
                if str(user_id).strip()
            )
            excluded_user_ids.update(
                str(user_id).strip()
                for user_id in worker_user_ids.values()
                if str(user_id).strip()
            )
            for resume_info in pending_slot_resumes.values():
                user_id = str(resume_info.get("user_id") or "").strip()
                if user_id:
                    excluded_user_ids.add(user_id)
            for launch_info in pending_backup_launches.values():
                old_user_id = str(launch_info.get("old_user_id") or "").strip()
                if old_user_id:
                    excluded_user_ids.add(old_user_id)
                replacement_user_id = str(launch_info.get("replacement_user_id") or "").strip()
                if replacement_user_id:
                    excluded_user_ids.add(replacement_user_id)
            for user_id in extra_user_ids:
                cleaned_user_id = str(user_id).strip()
                if cleaned_user_id:
                    excluded_user_ids.add(cleaned_user_id)
            return excluded_user_ids

        def _reserve_backup_launch_for_slot(
            slot_id: int,
            old_user_id: str,
            *,
            reason: str,
            proxy_label: str,
            launch_at: float,
            terminal_refill_requested: bool,
        ) -> bool:
            existing = pending_backup_launches.get(slot_id)
            if existing is not None:
                existing["old_user_id"] = old_user_id
                existing["reason"] = reason
                existing["proxy_label"] = proxy_label
                existing["launch_at"] = launch_at
                existing["terminal_refill_requested"] = bool(
                    existing.get("terminal_refill_requested", False)
                ) or terminal_refill_requested
                print(
                    f"Rescheduled pending backup for slot {slot_id} on proxy group {proxy_label} "
                    f"in {max(0.0, launch_at - time.monotonic()):.1f}s"
                )
                return True

            replacement_user_id = self.backup_pool_manager.reserve_backup_user_id(
                excluded_user_ids=_backup_reservation_exclusions(old_user_id)
            )
            pending_backup_launches[slot_id] = {
                "replacement_user_id": replacement_user_id,
                "old_user_id": old_user_id,
                "reason": reason,
                "proxy_label": proxy_label,
                "launch_at": launch_at,
                "terminal_refill_requested": terminal_refill_requested,
            }
            if replacement_user_id:
                self._mark_replaced(old_user_id, replacement_user_id)
                print(
                    f"Reserved backup {replacement_user_id} for slot {slot_id} after {reason}; "
                    f"launching on proxy group {proxy_label} in {max(0.0, launch_at - time.monotonic()):.1f}s"
                )
            else:
                print(
                    f"No backup available to reserve for slot {slot_id} after {reason}; "
                    f"proxy group {proxy_label} will remain queued until refill adds one"
                )
            return True

        def _schedule_pending_backups_for_group(
            proxy_label: str,
            *,
            stagger_launches: bool,
        ) -> None:
            recovery = proxy_group_recoveries.get(proxy_label)
            pending_backup_slots = list(recovery["pending_backup_slots"]) if recovery else []
            existing_launches = {
                slot_id: launch_info
                for slot_id, launch_info in pending_backup_launches.items()
                if str(launch_info["proxy_label"]) == proxy_label
            }
            if not pending_backup_slots and not existing_launches:
                return

            now = time.monotonic()
            first_launch_delay = max(0.0, self.config.proxy_rotation_first_resume_delay_seconds) if stagger_launches else 0.0
            stagger_seconds = max(0.0, self.config.proxy_rotation_resume_stagger_seconds) if stagger_launches else 0.0
            mode_label = "staggered" if stagger_launches else "immediate"
            combined_slots: dict[int, dict[str, str]] = {}
            for slot_info in pending_backup_slots:
                combined_slots[int(slot_info["slot_id"])] = {
                    "user_id": str(slot_info["user_id"]),
                    "reason": str(slot_info["reason"]),
                    "terminal_refill_requested": str(
                        bool(recovery.get("replacement_batch_requested", False))
                    ),
                }
            for slot_id, launch_info in existing_launches.items():
                combined_slots.setdefault(
                    int(slot_id),
                    {
                        "user_id": str(launch_info["old_user_id"]),
                        "reason": str(launch_info["reason"]),
                        "terminal_refill_requested": str(
                            bool(launch_info.get("terminal_refill_requested", False))
                        ),
                    },
                )
            print(
                f"Proxy group {proxy_label} rotation verified; scheduling {len(combined_slots)} "
                f"pending backups for {mode_label} launch"
            )
            for index, slot_id in enumerate(sorted(combined_slots)):
                slot_info = combined_slots[slot_id]
                old_user_id = str(slot_info["user_id"])
                reason = str(slot_info["reason"])
                launch_delay = first_launch_delay + (index * stagger_seconds)
                launch_at = now + launch_delay
                _reserve_backup_launch_for_slot(
                    slot_id,
                    old_user_id,
                    reason=reason,
                    proxy_label=proxy_label,
                    launch_at=launch_at,
                    terminal_refill_requested=(
                        str(slot_info.get("terminal_refill_requested", "False")).lower() == "true"
                    ),
                )

        def _drain_pending_backup_launches() -> None:
            if not pending_backup_launches:
                return
            now = time.monotonic()
            ready_slots = [slot_id for slot_id, launch_info in pending_backup_launches.items() if now >= float(launch_info["launch_at"])]
            for slot_id in sorted(ready_slots):
                if self.stop_event.is_set() or slot_id in workers:
                    continue
                launch_info = pending_backup_launches.get(slot_id)
                if not launch_info:
                    continue
                proxy_label = str(launch_info["proxy_label"])
                if proxy_label in proxy_group_recoveries:
                    continue
                if not launch_info.get("replacement_user_id"):
                    replacement_user_id = self.backup_pool_manager.reserve_backup_user_id(
                        excluded_user_ids=_backup_reservation_exclusions(
                            str(launch_info.get("old_user_id") or "")
                        )
                    )
                    if not replacement_user_id:
                        continue
                    launch_info["replacement_user_id"] = replacement_user_id
                    self._mark_replaced(str(launch_info["old_user_id"]), replacement_user_id)
                launch_info = pending_backup_launches.pop(slot_id, None)
                if not launch_info:
                    continue
                replacement_user_id = str(launch_info["replacement_user_id"])
                old_user_id = str(launch_info["old_user_id"])
                reason = str(launch_info["reason"])
                terminal_refill_requested = bool(launch_info.get("terminal_refill_requested", False))
                print(
                    f"Launching reserved backup for slot {slot_id} after {reason}; "
                    f"replacing {old_user_id} with {replacement_user_id}"
                )
                _launch_backup_for_slot(
                    slot_id,
                    old_user_id,
                    reason=reason,
                    terminal_refill_requested=terminal_refill_requested,
                    preselected_replacement_user_id=replacement_user_id,
                    started_after_rotation_resume=True,
                )

        def _is_post_rotation_match_error_result(result: dict) -> bool:
            fatal_error = str(result.get("fatal_error") or "").lower()
            if not fatal_error:
                return False
            return "api abuse match-error persisted" in fatal_error

        def _is_obvious_shared_infra_failure(result: dict) -> bool:
            fatal_error = str(result.get("fatal_error") or "").lower()
            if not fatal_error:
                return False
            infra_markers = (
                "texting_proxy_replacement_failed",
                "texting_proxy_not_configured",
                "err_proxy_auth_unsupported",
                "err_tunnel_connection_failed",
                "failed to establish a new connection",
                "actively refused it",
                "forcibly closed by the remote host",
                "httpconnectionpool(host='localhost'",
                "httpconnection(host='localhost'",
                "proxy rotation",
                "shared_infra_collapse:",
                "api abuse match-error persisted",
                "match-connection error persisted after refresh recovery attempts",
                "socket/server error persisted after refresh recovery attempts",
                "press-start state repeated without confirmed active chat",
                "press-start state persisted without confirmed active chat",
                "loading state persisted after refresh recovery attempts",
            )
            return any(marker in fatal_error for marker in infra_markers)

        def _should_launch_backup_for_result(slot_id: int, result: dict) -> bool:
            status = str(result.get("status") or "")
            fatal_error = str(result.get("fatal_error") or "")

            if status == "banned":
                return True
            if fatal_error == "REGISTRATION_FAILED":
                return True
            if status in {"failed", "max_retries"}:
                return True
            return False

        def _is_shared_infra_collapse_result(result: dict) -> bool:
            status = str(result.get("status") or "")
            if status == "parked":
                return True
            return _is_obvious_shared_infra_failure(result)

        def _launch_backup_for_slot(
            slot_id: int,
            old_user_id: str,
            *,
            reason: str,
            terminal_refill_requested: bool = False,
            preselected_replacement_user_id: str | None = None,
            started_after_rotation_resume: bool = False,
        ) -> bool:
            browser_closed = _stop_profile_browser_for_retirement(
                old_user_id,
                reason=f"slot replacement after {reason}",
            )
            if not browser_closed:
                print(
                    f"Profile {old_user_id} did not confirm browser shutdown before slot {slot_id} "
                    f"replacement after {reason}; continuing with refill retirement cleanup"
                )

            excluded_user_ids = _backup_reservation_exclusions(old_user_id)
            if preselected_replacement_user_id and preselected_replacement_user_id in excluded_user_ids:
                preselected_replacement_user_id = None
            replacement_user_id = (
                preselected_replacement_user_id
                or self.backup_pool_manager.reserve_backup_user_id(
                    excluded_user_ids=excluded_user_ids
                )
            )
            if not replacement_user_id:
                proxy_label = str(slot_proxy_labels.get(slot_id) or "")
                pending_backup_launches[slot_id] = {
                    "replacement_user_id": None,
                    "old_user_id": old_user_id,
                    "reason": reason,
                    "proxy_label": proxy_label,
                    "launch_at": time.monotonic(),
                    "terminal_refill_requested": terminal_refill_requested,
                }
                refill_batch_id = None
                if not terminal_refill_requested:
                    retired_user_ids = [old_user_id] if self.config.delete_terminal_profiles else []
                    refill_batch_id = self.backup_pool_manager.enqueue_refill_batch(
                        retired_user_ids,
                        requested_count=1,
                        proxy_label=proxy_label or None,
                        reason=f"slot-replacement:{reason}",
                    )
                print(
                    f"No live backup is available for slot {slot_id} after {reason}; "
                    "keeping replacement queued until refill prepares another backup"
                )
                if refill_batch_id:
                    print(
                        f"Queued dynamic refill batch {refill_batch_id} for slot {slot_id} "
                        f"after {reason}; recycling failed profile {old_user_id}"
                    )
                return False
            print(
                f"Launching backup for slot {slot_id} after {reason}; "
                f"replacing {old_user_id} with {replacement_user_id}"
            )
            _start_worker_for_slot(
                slot_id,
                replacement_user_id,
                is_backup=True,
                started_after_rotation_resume=started_after_rotation_resume,
            )
            self._mark_replaced(old_user_id, replacement_user_id)
            completed_slots.discard(slot_id)
            slot_dead_worker_counts[slot_id] = 0
            if not terminal_refill_requested:
                proxy_label = str(slot_proxy_labels.get(slot_id) or "")
                retired_user_ids = [old_user_id] if self.config.delete_terminal_profiles else []
                refill_batch_id = self.backup_pool_manager.enqueue_refill_batch(
                    retired_user_ids,
                    requested_count=1,
                    proxy_label=proxy_label or None,
                    reason=f"slot-replacement:{reason}",
                )
                if refill_batch_id:
                    print(
                        f"Queued dynamic refill batch {refill_batch_id} to replenish backup stock "
                        f"after slot {slot_id} {reason}; retiring {old_user_id}"
                    )
            return True

        def _is_user_reusable_for_export(user_id: str | None) -> bool:
            if not user_id:
                return False
            if user_id in ban_pending_users:
                return False
            record = self.summary.get(user_id)
            if not record:
                return True
            if record.get("replaced_by"):
                return False
            if record.get("status") == "banned":
                return False
            if record.get("status") == "parked":
                return False
            if record.get("fatal_error") == "REGISTRATION_FAILED":
                return False
            if record.get("registration_attempted") and record.get("registration_ok") is not True:
                return False
            if record.get("status") in {"failed", "max_retries"}:
                return False
            return True

        def _count_uncovered_pending_replacement_slots() -> int:
            covered_slots = {
                int(slot_id)
                for slot_id, launch_info in pending_backup_launches.items()
                if launch_info.get("replacement_user_id")
            }
            uncovered_slots = {
                int(slot_id)
                for slot_id, launch_info in pending_backup_launches.items()
                if not launch_info.get("replacement_user_id")
            }
            for recovery in proxy_group_recoveries.values():
                for slot_info in recovery.get("pending_backup_slots") or []:
                    slot_id = int(slot_info["slot_id"])
                    if slot_id not in covered_slots:
                        uncovered_slots.add(slot_id)
            return len(uncovered_slots)

        def _covered_slot_ids() -> set[int]:
            covered_slots = set(int(slot_id) for slot_id in workers)
            covered_slots.update(int(slot_id) for slot_id in pending_slot_resumes)
            covered_slots.update(int(slot_id) for slot_id in pending_backup_launches)
            for recovery in proxy_group_recoveries.values():
                covered_slots.update(
                    int(slot_info["slot_id"])
                    for slot_info in recovery.get("pending_backup_slots") or []
                )
                covered_slots.update(
                    int(slot_info["slot_id"])
                    for slot_info in recovery.get("resume_candidates") or []
                )
                canary_slot_id = recovery.get("canary_slot_id")
                if canary_slot_id is not None:
                    covered_slots.add(int(canary_slot_id))
            return covered_slots

        def _reconcile_slot_liveness() -> None:
            if self.stop_event.is_set():
                return
            covered_slots = _covered_slot_ids()
            for slot_id in range(len(self.config.user_ids)):
                if slot_id in covered_slots:
                    continue
                current_user_id = str(
                    slot_current_user_ids.get(slot_id)
                    or worker_user_ids.get(slot_id)
                    or self.config.user_ids[slot_id]
                ).strip()
                if not current_user_id:
                    continue
                print(
                    f"Slot {slot_id} has no active worker or queued recovery state; "
                    "launching replacement to preserve indefinite run"
                )
                _launch_backup_for_slot(
                    slot_id,
                    current_user_id,
                    reason="slot liveness reconciliation",
                )

        def _reconcile_backup_capacity() -> None:
            nonlocal next_capacity_refill_at
            if not self.config.capacity_reconciliation_enabled:
                return
            if not self.config.dynamic_backup_refill_enabled:
                return
            now = time.monotonic()
            if now < next_capacity_refill_at:
                return

            target_ready_backups = max(0, int(self.config.target_ready_backup_count))
            available_backups = len(self.backup_pool_manager.available_backup_user_ids())
            pending_backup_output = int(self.backup_pool_manager.pending_backup_output_count())
            uncovered_pending_replacements = _count_uncovered_pending_replacement_slots()
            # Keep the configured ready-backup target, but do not stack uncovered
            # replacement demand on top of it. Otherwise a full 10-slot outage with
            # BOT_TARGET_READY_BACKUP_COUNT=10 asks for 20 profiles at once.
            desired_backups = max(target_ready_backups, uncovered_pending_replacements)
            if available_backups + pending_backup_output > desired_backups:
                trimmed_batches = self.backup_pool_manager.trim_excess_pending_output(desired_backups)
                if trimmed_batches > 0:
                    available_backups = len(self.backup_pool_manager.available_backup_user_ids())
                    pending_backup_output = int(self.backup_pool_manager.pending_backup_output_count())
            missing_backups = max(0, desired_backups - (available_backups + pending_backup_output))
            if missing_backups <= 0:
                return

            refill_batch_id = self.backup_pool_manager.enqueue_refill_batch(
                [],
                requested_count=missing_backups,
                proxy_label=None,
                reason="capacity-top-up",
            )
            if not refill_batch_id:
                return
            next_capacity_refill_at = now + max(5.0, float(self.config.restart_backoff_seconds))
            print(
                "Queued dynamic backup top-up batch "
                f"{refill_batch_id}: missing_backups={missing_backups} "
                f"target_ready_backups={target_ready_backups} "
                f"available_backups={available_backups} "
                f"pending_output={pending_backup_output} "
                f"uncovered_pending_replacements={uncovered_pending_replacements}"
            )

        def _update_final_env_exports() -> None:
            ordered_slot_user_ids: list[str] = []
            for slot_id in range(len(self.config.user_ids)):
                if slot_id in worker_user_ids:
                    candidate_user_id = worker_user_ids[slot_id]
                    if _is_user_reusable_for_export(candidate_user_id):
                        ordered_slot_user_ids.append(candidate_user_id)
                    continue
                resume_info = pending_slot_resumes.get(slot_id)
                if resume_info is not None:
                    candidate_user_id = str(resume_info["user_id"])
                    if _is_user_reusable_for_export(candidate_user_id):
                        ordered_slot_user_ids.append(candidate_user_id)
                    continue
                backup_launch_info = pending_backup_launches.get(slot_id)
                if backup_launch_info is not None:
                    candidate_user_id = str(backup_launch_info.get("replacement_user_id") or "")
                    if _is_user_reusable_for_export(candidate_user_id):
                        ordered_slot_user_ids.append(candidate_user_id)
                    continue
                current_user_id = slot_current_user_ids.get(slot_id)
                if _is_user_reusable_for_export(current_user_id):
                    ordered_slot_user_ids.append(current_user_id)
            self.final_slot_user_ids_csv = ",".join(ordered_slot_user_ids)
            self.remaining_backup_user_ids_csv = ",".join(
                self.backup_pool_manager.available_backup_user_ids()
            )

        def _queue_proxy_group_recovery(
            proxy_label: str,
            proxy_value: str,
            *,
            banned_slot_id: int,
            banned_user_id: str,
            resume_candidates: list[dict[str, object]],
            triaged_banned_slots: list[dict[str, object]],
            stagger_pending_backup_launches: bool = False,
        ) -> None:
            pending_backup_slots = [
                {
                    "slot_id": banned_slot_id,
                    "user_id": banned_user_id,
                    "reason": "ban",
                }
            ]
            for slot_info in triaged_banned_slots:
                pending_backup_slots.append(
                    {
                        "slot_id": int(slot_info["slot_id"]),
                        "user_id": str(slot_info["user_id"]),
                        "reason": "pre-rotation triage ban",
                    }
                )
            proxy_group_recoveries[proxy_label] = {
                "proxy_label": proxy_label,
                "proxy_value": proxy_value,
                "resume_candidates": [dict(item) for item in resume_candidates],
                "pending_backup_slots": pending_backup_slots,
                "next_retry_at": time.monotonic(),
                "backoff_seconds": max(1.0, self.config.proxy_rotation_recovery_initial_backoff_seconds),
                "ready_to_probe": False,
                "canary_slot_id": None,
                "canary_user_id": None,
                "canary_attempts_used": 0,
                "stagger_pending_backup_launches": stagger_pending_backup_launches,
                "replacement_batch_requested": False,
            }
            print(
                f"Queued proxy group {proxy_label} for parked recovery: "
                f"survivors={len(resume_candidates)} pending_backups={len(pending_backup_slots)}"
            )

        def _add_banned_slot_to_recovery(
            proxy_label: str,
            slot_id: int,
            user_id: str,
            *,
            reason: str,
        ) -> bool:
            recovery = proxy_group_recoveries.get(proxy_label)
            if not recovery:
                return False
            _remove_resume_candidate(recovery, slot_id)
            pending_slot_resumes.pop(slot_id, None)
            if not any(int(item["slot_id"]) == slot_id for item in recovery["pending_backup_slots"]):
                recovery["pending_backup_slots"].append(
                    {
                        "slot_id": slot_id,
                        "user_id": user_id,
                        "reason": reason,
                    }
                )
            return True

        def _queue_proxy_group_recovery_burn_all(
            proxy_label: str,
            proxy_value: str,
            *,
            banned_slot_id: int,
            banned_user_id: str,
        ) -> None:
            existing_recovery = proxy_group_recoveries.get(proxy_label)
            pending_backup_slots: list[dict[str, object]] = []
            already_enqueued_slot_ids: set[int] = set()
            newly_banned_user_ids: list[str] = []
            if existing_recovery:
                for slot_info in list(existing_recovery["pending_backup_slots"]):
                    already_enqueued_slot_ids.add(int(slot_info["slot_id"]))
                    _append_pending_backup_slot(
                        pending_backup_slots,
                        slot_id=int(slot_info["slot_id"]),
                        user_id=str(slot_info["user_id"]),
                        reason=str(slot_info["reason"]),
                    )
                for slot_info in list(existing_recovery["resume_candidates"]):
                    slot_id = int(slot_info["slot_id"])
                    user_id = str(slot_info["user_id"])
                    _mark_banned_slot(slot_id, user_id)
                    _append_pending_backup_slot(
                        pending_backup_slots,
                        slot_id=slot_id,
                        user_id=user_id,
                        reason="burn-all survivor invalidation",
                    )
                    if slot_id not in already_enqueued_slot_ids:
                        newly_banned_user_ids.append(user_id)

            _append_pending_backup_slot(
                pending_backup_slots,
                slot_id=banned_slot_id,
                user_id=banned_user_id,
                reason="ban",
            )
            if banned_slot_id not in already_enqueued_slot_ids:
                newly_banned_user_ids.append(banned_user_id)

            trigger_browser_closed = _terminate_slot_worker_immediately(
                banned_slot_id,
                banned_user_id,
                reason="burn-all ban quarantine",
                mark_banned=True,
            )

            quarantined_slots = _quarantine_proxy_group(
                proxy_label,
                banned_slot_id,
                mark_all_as_banned=True,
            )
            for slot_info in quarantined_slots:
                slot_id = int(slot_info["slot_id"])
                user_id = str(slot_info["user_id"])
                _append_pending_backup_slot(
                    pending_backup_slots,
                    slot_id=slot_id,
                    user_id=user_id,
                    reason="burn-all group quarantine",
                )
                if slot_id not in already_enqueued_slot_ids:
                    newly_banned_user_ids.append(user_id)

            shutdown_pending_user_ids = []
            if not trigger_browser_closed:
                shutdown_pending_user_ids.append(banned_user_id)
            for slot_info in quarantined_slots:
                user_id = str(slot_info["user_id"])
                if not bool(slot_info.get("browser_closed", False)):
                    shutdown_pending_user_ids.append(user_id)

            pending_resume_slot_ids = [
                slot_id
                for slot_id, resume_info in pending_slot_resumes.items()
                if str(resume_info["proxy_label"]) == proxy_label
            ]
            for slot_id in sorted(pending_resume_slot_ids):
                resume_info = pending_slot_resumes.pop(slot_id, None)
                if resume_info is None:
                    continue
                user_id = str(resume_info["user_id"])
                _mark_banned_slot(slot_id, user_id)
                _append_pending_backup_slot(
                    pending_backup_slots,
                    slot_id=slot_id,
                    user_id=user_id,
                    reason="burn-all queued survivor invalidation",
                )
                if slot_id not in already_enqueued_slot_ids:
                    newly_banned_user_ids.append(user_id)

            proxy_group_recoveries[proxy_label] = {
                "proxy_label": proxy_label,
                "proxy_value": proxy_value,
                "resume_candidates": [],
                "pending_backup_slots": pending_backup_slots,
                "next_retry_at": time.monotonic(),
                "backoff_seconds": max(1.0, self.config.proxy_rotation_recovery_initial_backoff_seconds),
                "ready_to_probe": False,
                "canary_slot_id": None,
                "canary_user_id": None,
                "canary_attempts_used": 0,
                "stagger_pending_backup_launches": True,
                "replacement_batch_requested": True,
                "shutdown_pending_user_ids": sorted(set(shutdown_pending_user_ids)),
            }
            refill_batch_id = self.backup_pool_manager.enqueue_refill_batch(
                newly_banned_user_ids if self.config.delete_terminal_profiles else [],
                requested_count=len(newly_banned_user_ids),
                proxy_label=proxy_label,
                reason="burn-all-replacement",
            )
            print(
                f"Queued proxy group {proxy_label} for burn-all parked recovery: "
                f"survivors=0 pending_backups={len(pending_backup_slots)} "
                f"refill_batch={refill_batch_id or '<none>'}"
            )

        def _queue_proxy_group_recovery_shared_collapse(
            proxy_label: str,
            proxy_value: str,
            *,
            trigger_slot_id: int,
            trigger_user_id: str,
            trigger_reason: str,
        ) -> None:
            existing_recovery = proxy_group_recoveries.get(proxy_label)
            pending_backup_slots: list[dict[str, object]] = []
            already_enqueued_slot_ids: set[int] = set()
            newly_parked_user_ids: list[str] = []
            shutdown_pending_user_ids: list[str] = []

            if existing_recovery:
                for slot_info in list(existing_recovery["pending_backup_slots"]):
                    already_enqueued_slot_ids.add(int(slot_info["slot_id"]))
                    _append_pending_backup_slot(
                        pending_backup_slots,
                        slot_id=int(slot_info["slot_id"]),
                        user_id=str(slot_info["user_id"]),
                        reason=str(slot_info["reason"]),
                    )
                for slot_info in list(existing_recovery["resume_candidates"]):
                    slot_id = int(slot_info["slot_id"])
                    user_id = str(slot_info["user_id"])
                    _append_pending_backup_slot(
                        pending_backup_slots,
                        slot_id=slot_id,
                        user_id=user_id,
                        reason="shared collapse survivor invalidation",
                    )
                    if slot_id not in already_enqueued_slot_ids:
                        newly_parked_user_ids.append(user_id)
                shutdown_pending_user_ids.extend(
                    str(user_id).strip()
                    for user_id in existing_recovery.get("shutdown_pending_user_ids") or []
                    if str(user_id).strip()
                )

            _append_pending_backup_slot(
                pending_backup_slots,
                slot_id=trigger_slot_id,
                user_id=trigger_user_id,
                reason=trigger_reason,
            )
            if trigger_slot_id not in already_enqueued_slot_ids:
                newly_parked_user_ids.append(trigger_user_id)

            if not _stop_profile_browser_for_retirement(
                trigger_user_id,
                reason="shared collapse trigger quarantine",
            ):
                shutdown_pending_user_ids.append(trigger_user_id)

            quarantined_slots = _quarantine_proxy_group(proxy_label, trigger_slot_id)
            for slot_info in quarantined_slots:
                slot_id = int(slot_info["slot_id"])
                user_id = str(slot_info["user_id"])
                _append_pending_backup_slot(
                    pending_backup_slots,
                    slot_id=slot_id,
                    user_id=user_id,
                    reason="shared collapse group quarantine",
                )
                if slot_id not in already_enqueued_slot_ids:
                    newly_parked_user_ids.append(user_id)
                if not bool(slot_info.get("browser_closed", False)):
                    shutdown_pending_user_ids.append(user_id)

            pending_resume_slot_ids = [
                slot_id
                for slot_id, resume_info in pending_slot_resumes.items()
                if str(resume_info["proxy_label"]) == proxy_label
            ]
            for slot_id in sorted(pending_resume_slot_ids):
                resume_info = pending_slot_resumes.pop(slot_id, None)
                if resume_info is None:
                    continue
                user_id = str(resume_info["user_id"])
                _append_pending_backup_slot(
                    pending_backup_slots,
                    slot_id=slot_id,
                    user_id=user_id,
                    reason="shared collapse queued survivor invalidation",
                )
                if slot_id not in already_enqueued_slot_ids:
                    newly_parked_user_ids.append(user_id)

            proxy_group_recoveries[proxy_label] = {
                "proxy_label": proxy_label,
                "proxy_value": proxy_value,
                "resume_candidates": [],
                "pending_backup_slots": pending_backup_slots,
                "next_retry_at": time.monotonic(),
                "backoff_seconds": max(1.0, self.config.proxy_rotation_recovery_initial_backoff_seconds),
                "ready_to_probe": False,
                "canary_slot_id": None,
                "canary_user_id": None,
                "canary_attempts_used": 0,
                "stagger_pending_backup_launches": True,
                "replacement_batch_requested": True,
                "shutdown_pending_user_ids": sorted(set(shutdown_pending_user_ids)),
            }
            refill_batch_id = self.backup_pool_manager.enqueue_refill_batch(
                [],
                requested_count=len(set(newly_parked_user_ids)),
                proxy_label=proxy_label,
                reason="shared-infra-collapse",
            )
            print(
                f"Queued proxy group {proxy_label} for shared-collapse parked recovery: "
                f"survivors=0 pending_backups={len(pending_backup_slots)} "
                f"refill_batch={refill_batch_id or '<none>'}"
            )

        def _handle_ban_signal(result: dict) -> None:
            slot_id = int(result["slot_id"])
            user_id = str(result["user_id"])
            if _should_ignore_stale_slot_event(slot_id, user_id):
                print(
                    f"Ignoring stale ban signal for slot {slot_id} ({user_id}); "
                    "profile was already replaced or is no longer current"
                )
                return
            proxy_label = (
                str(result.get("assigned_texting_proxy_label") or "")
                or str(slot_proxy_labels.get(slot_id) or "")
            )
            proxy_value = slot_proxy_values.get(slot_id)
            _mark_banned_slot(slot_id, user_id)
            if (
                self.config.proxy_rotation_enabled
                and self.config.proxy_rotation_recovery_enabled
                and proxy_label
                and proxy_value
            ):
                if not self.config.proxy_rotation_triage_survivors_on_ban:
                    _queue_proxy_group_recovery_burn_all(
                        proxy_label,
                        proxy_value,
                        banned_slot_id=slot_id,
                        banned_user_id=user_id,
                    )
                    return
                if _add_banned_slot_to_recovery(
                    proxy_label,
                    slot_id,
                    user_id,
                    reason="ban",
                ):
                    return
                quarantined_slots = _quarantine_proxy_group(proxy_label, slot_id)
                triaged_banned_slots, resume_candidates = _triage_quarantined_slots_for_bans(
                    proxy_label,
                    quarantined_slots,
                )
                _queue_proxy_group_recovery(
                    proxy_label,
                    proxy_value,
                    banned_slot_id=slot_id,
                    banned_user_id=user_id,
                    resume_candidates=resume_candidates,
                    triaged_banned_slots=triaged_banned_slots,
                )

        def _schedule_proxy_group_recovery_retry(proxy_label: str, reason: str) -> None:
            recovery = proxy_group_recoveries.get(proxy_label)
            if not recovery:
                return
            current_backoff = float(recovery["backoff_seconds"])
            next_backoff = min(
                max(1.0, self.config.proxy_rotation_recovery_max_backoff_seconds),
                max(1.0, current_backoff * 2),
            )
            recovery["next_retry_at"] = time.monotonic() + current_backoff
            recovery["backoff_seconds"] = next_backoff
            recovery["ready_to_probe"] = False
            recovery["canary_slot_id"] = None
            recovery["canary_user_id"] = None
            recovery["canary_attempts_used"] = 0
            print(
                f"Proxy group {proxy_label} parked for recovery after {reason}; "
                f"retrying in {current_backoff:.1f}s"
            )

        def _launch_pending_backups_for_group(proxy_label: str) -> None:
            recovery = proxy_group_recoveries.get(proxy_label)
            if not recovery:
                return
            for slot_info in recovery["pending_backup_slots"]:
                _launch_backup_for_slot(
                    int(slot_info["slot_id"]),
                    str(slot_info["user_id"]),
                    reason=str(slot_info["reason"]),
                    terminal_refill_requested=bool(recovery.get("replacement_batch_requested", False)),
                    started_after_rotation_resume=True,
                )

        def _activate_recovered_proxy_group(proxy_label: str) -> None:
            recovery = proxy_group_recoveries.get(proxy_label)
            if not recovery:
                return
            canary_slot_id = recovery.get("canary_slot_id")
            canary_user_id = recovery.get("canary_user_id")
            if canary_slot_id is not None and canary_user_id is not None:
                _start_worker_for_slot(
                    int(canary_slot_id),
                    str(canary_user_id),
                    is_backup=bool(self.summary.get(str(canary_user_id), {}).get("is_backup", False)),
                    started_after_rotation_resume=True,
                )
                completed_slots.discard(int(canary_slot_id))

            remaining_resume_candidates = [
                slot_info
                for slot_info in recovery["resume_candidates"]
                if int(slot_info["slot_id"]) != recovery.get("canary_slot_id")
            ]
            if bool(recovery.get("stagger_pending_backup_launches")):
                _schedule_pending_backups_for_group(proxy_label, stagger_launches=True)
            else:
                _launch_pending_backups_for_group(proxy_label)
            if self.config.proxy_rotation_auto_resume_quarantined_slots:
                _schedule_quarantined_slot_resumes(proxy_label, remaining_resume_candidates)
            proxy_group_recoveries.pop(proxy_label, None)

        def _attempt_rotate_proxy_group(proxy_label: str, proxy_value: str) -> str:
            rotation_link = proxy_label_to_rotation_link.get(proxy_label)
            if not rotation_link:
                print(f"No rotation link configured for proxy group {proxy_label}")
                return "failed"

            previous_consensus: dict[str, str | None] | None = None
            previous_responses: dict[str, dict[str, str]] = {"ipv4": {}, "ipv6": {}}
            previous_errors: dict[str, dict[str, str]] = {"ipv4": {}, "ipv6": {}}
            if self.config.proxy_rotation_verify_ip_change:
                previous_consensus, previous_responses, previous_errors = _fetch_proxy_exit_ip_consensus_pair(
                    self.config,
                    proxy_value,
                )
                print(
                    f"Proxy group {proxy_label} current exit IP consensus before rotation: "
                    f"ipv4={previous_consensus['ipv4'] or '<inconclusive>'} "
                    f"ipv6={previous_consensus['ipv6'] or '<inconclusive>'}"
                )
                if previous_responses["ipv4"] or previous_responses["ipv6"]:
                    print(
                        f"Proxy group {proxy_label} pre-rotation provider readings: "
                        f"ipv4={previous_responses['ipv4']} ipv6={previous_responses['ipv6']}"
                    )
                if previous_errors["ipv4"] or previous_errors["ipv6"]:
                    print(
                        f"Proxy group {proxy_label} pre-rotation provider errors: "
                        f"ipv4={previous_errors['ipv4']} ipv6={previous_errors['ipv6']}"
                    )

            triggered_successfully = False
            rotation_attempts = max(1, self.config.proxy_rotation_attempts)
            trigger_burst_count = max(1, int(self.config.proxy_rotation_trigger_burst_count))
            trigger_burst_spacing = max(0.0, float(self.config.proxy_rotation_trigger_burst_spacing_seconds))
            for rotation_attempt in range(1, rotation_attempts + 1):
                burst_success = False
                for burst_index in range(1, trigger_burst_count + 1):
                    try:
                        trigger_result = _trigger_proxy_rotation_link(self.config, rotation_link)
                        triggered_successfully = True
                        burst_success = True
                        acknowledged = bool(trigger_result.get("acknowledged"))
                        response_message = trigger_result.get("message")
                        body_snippet = str(trigger_result.get("body_snippet") or "")
                        if acknowledged:
                            print(
                                f"Triggered rotation link for proxy group {proxy_label} "
                                f"(attempt {rotation_attempt}/{rotation_attempts}, burst {burst_index}/{trigger_burst_count}); "
                                f"endpoint acknowledged rotation command: {response_message!r}"
                            )
                        else:
                            print(
                                f"Triggered rotation link for proxy group {proxy_label} "
                                f"(attempt {rotation_attempt}/{rotation_attempts}, burst {burst_index}/{trigger_burst_count}); "
                                f"status={trigger_result.get('status_code')} body={body_snippet!r}"
                            )
                    except Exception as error:  # noqa: BLE001
                        print(
                            f"Failed to trigger rotation for proxy group {proxy_label} "
                            f"on attempt {rotation_attempt}/{rotation_attempts}, "
                            f"burst {burst_index}/{trigger_burst_count}: {error}"
                        )
                    if burst_index < trigger_burst_count:
                        time.sleep(trigger_burst_spacing)

                if not burst_success:
                    if rotation_attempt >= rotation_attempts:
                        return "failed"
                    time.sleep(max(0.0, self.config.proxy_rotation_retry_cooldown_seconds))
                    continue

                time.sleep(max(0.0, self.config.proxy_rotation_post_trigger_delay_seconds))

                if not self.config.proxy_rotation_verify_ip_change:
                    return "confirmed"

                deadline = time.monotonic() + max(1.0, self.config.proxy_rotation_verify_timeout_seconds)
                last_consensus: dict[str, str | None] | None = None
                last_responses: dict[str, dict[str, str]] = {"ipv4": {}, "ipv6": {}}
                last_errors: dict[str, dict[str, str]] = {"ipv4": {}, "ipv6": {}}
                while time.monotonic() < deadline:
                    last_consensus, last_responses, last_errors = _fetch_proxy_exit_ip_consensus_pair(
                        self.config,
                        proxy_value,
                    )
                    ipv4_changed = bool(
                        previous_consensus
                        and previous_consensus["ipv4"]
                        and last_consensus["ipv4"]
                        and last_consensus["ipv4"] != previous_consensus["ipv4"]
                    )
                    ipv6_changed = bool(
                        previous_consensus
                        and previous_consensus["ipv6"]
                        and last_consensus["ipv6"]
                        and last_consensus["ipv6"] != previous_consensus["ipv6"]
                    )
                    changed_stack_messages: list[str] = []
                    if ipv4_changed:
                        changed_stack_messages.append(
                            f"ipv4={previous_consensus['ipv4']} -> {last_consensus['ipv4']}"
                        )
                    if ipv6_changed:
                        changed_stack_messages.append(
                            f"ipv6={previous_consensus['ipv6']} -> {last_consensus['ipv6']}"
                        )
                    if changed_stack_messages:
                        print(
                            f"Proxy group {proxy_label} exit IP consensus changed after rotation "
                            f"on confirmed stack(s): {' | '.join(changed_stack_messages)}"
                        )
                        return "confirmed"
                    time.sleep(max(0.5, self.config.proxy_rotation_verify_poll_seconds))

                print(
                    f"Proxy group {proxy_label} trigger succeeded but exit IP consensus did not confirm "
                    f"an IP change on any stack "
                    f"on attempt {rotation_attempt}/{rotation_attempts}; "
                    f"post-rotation consensus ipv4={last_consensus['ipv4'] if last_consensus else '<inconclusive>'} "
                    f"ipv6={last_consensus['ipv6'] if last_consensus else '<inconclusive>'}"
                )
                if last_responses["ipv4"] or last_responses["ipv6"]:
                    print(
                        f"Proxy group {proxy_label} post-rotation provider readings: "
                        f"ipv4={last_responses['ipv4']} ipv6={last_responses['ipv6']}"
                    )
                if last_errors["ipv4"] or last_errors["ipv6"]:
                    print(
                        f"Proxy group {proxy_label} post-rotation provider errors: "
                        f"ipv4={last_errors['ipv4']} ipv6={last_errors['ipv6']}"
                    )

                if rotation_attempt < rotation_attempts:
                    print(
                        f"Waiting {self.config.proxy_rotation_retry_cooldown_seconds:.1f}s "
                        f"before retrying proxy group {proxy_label} rotation"
                    )
                    time.sleep(max(0.0, self.config.proxy_rotation_retry_cooldown_seconds))

            return "inconclusive" if triggered_successfully else "failed"

        def _start_group_canary_probe(proxy_label: str) -> bool:
            recovery = proxy_group_recoveries.get(proxy_label)
            if not recovery:
                return False
            if not self.config.proxy_rotation_triage_survivors_on_ban:
                _activate_recovered_proxy_group(proxy_label)
                return True
            if not self.config.proxy_rotation_canary_enabled:
                _activate_recovered_proxy_group(proxy_label)
                return True
            resume_candidates = recovery["resume_candidates"]
            if not resume_candidates:
                _launch_pending_backups_for_group(proxy_label)
                proxy_group_recoveries.pop(proxy_label, None)
                return True
            canary_slot = resume_candidates[0]
            recovery["canary_slot_id"] = int(canary_slot["slot_id"])
            recovery["canary_user_id"] = str(canary_slot["user_id"])
            recovery["canary_attempts_used"] = int(recovery["canary_attempts_used"]) + 1
            probe_slot_proxy_labels[int(canary_slot["slot_id"])] = proxy_label
            _start_worker_for_slot(
                int(canary_slot["slot_id"]),
                str(canary_slot["user_id"]),
                is_backup=bool(canary_slot["is_backup"]),
                started_after_rotation_resume=True,
                probe_only=True,
            )
            print(
                f"Launching survivor-only canary probe for proxy group {proxy_label}: "
                f"slot {canary_slot['slot_id']} ({canary_slot['user_id']}) "
                f"[attempt {recovery['canary_attempts_used']}/{self.config.proxy_rotation_canary_attempts_per_wave}]"
            )
            return True

        def _find_recovery_for_canary_slot(slot_id: int) -> tuple[str | None, dict[str, object] | None]:
            for proxy_label, recovery in proxy_group_recoveries.items():
                if int(recovery.get("canary_slot_id") or -1) == slot_id:
                    return proxy_label, recovery
            return None, None

        def _remove_resume_candidate(recovery: dict[str, object], slot_id: int) -> None:
            recovery["resume_candidates"] = [
                slot_info
                for slot_info in recovery["resume_candidates"]
                if int(slot_info["slot_id"]) != slot_id
            ]

        def _handle_canary_probe_result(result: dict) -> bool:
            slot_id = int(result["slot_id"])
            proxy_label, recovery = _find_recovery_for_canary_slot(slot_id)
            if not recovery or proxy_label is None:
                fallback_proxy_label = (
                    probe_slot_proxy_labels.get(slot_id)
                    or str(result.get("assigned_texting_proxy_label") or "")
                )
                fallback_recovery = proxy_group_recoveries.get(fallback_proxy_label) if fallback_proxy_label else None
                fallback_matches_slot = bool(
                    fallback_recovery
                    and (
                        int(fallback_recovery.get("canary_slot_id") or -1) == slot_id
                        or any(
                            int(slot_info["slot_id"]) == slot_id
                            for slot_info in fallback_recovery["resume_candidates"]
                        )
                    )
                )
                if fallback_recovery and fallback_matches_slot:
                    proxy_label = fallback_proxy_label
                    recovery = fallback_recovery
                    print(
                        f"Recovered canary probe result for proxy group {proxy_label}: "
                        f"slot {slot_id} ({result['user_id']}) matched via probe ownership"
                    )

            if slot_probe_modes.get(slot_id) != "canary_probe" and not bool(result.get("probe_only")):
                return False

            if not recovery or proxy_label is None:
                # A late-arriving probe result can race with dead-worker cleanup after the
                # recovery bookkeeping has already been cleared. Swallow it so it does not get
                # mistaken for a normal slot result.
                slot_probe_modes.pop(slot_id, None)
                probe_slot_proxy_labels.pop(slot_id, None)
                return True

            slot_probe_modes.pop(slot_id, None)
            probe_slot_proxy_labels.pop(slot_id, None)

            recovery["canary_slot_id"] = None
            recovery["canary_user_id"] = None

            status = str(result.get("status") or "")
            fatal_error = str(result.get("fatal_error") or "")
            user_id = str(result["user_id"])

            if status == "success":
                print(
                    f"Canary probe for proxy group {proxy_label} succeeded with slot {slot_id} ({user_id}); "
                    "releasing survivors and pending backups"
                )
                _activate_recovered_proxy_group(proxy_label)
                return True

            if status == "banned" or fatal_error == "REGISTRATION_FAILED":
                reason = "canary survivor ban" if status == "banned" else "canary survivor registration failure"
                print(
                    f"Canary probe for proxy group {proxy_label} hard-failed in slot {slot_id} ({user_id}): "
                    f"{status or fatal_error}; removing survivor from this recovery wave"
                )
                _remove_resume_candidate(recovery, slot_id)
                if not any(int(item["slot_id"]) == slot_id for item in recovery["pending_backup_slots"]):
                    recovery["pending_backup_slots"].append(
                        {
                            "slot_id": slot_id,
                            "user_id": user_id,
                            "reason": reason,
                        }
                    )
                recovery["ready_to_probe"] = False
                recovery["next_retry_at"] = time.monotonic() + max(
                    0.0,
                    self.config.proxy_rotation_canary_retry_delay_seconds,
                )
                if not recovery["resume_candidates"]:
                    print(
                        f"Proxy group {proxy_label} has no survivor canary candidates left; "
                        "the next successful recovery wave will launch pending backups directly"
                    )
                return True

            attempts_used = int(recovery["canary_attempts_used"])
            max_attempts = max(1, self.config.proxy_rotation_canary_attempts_per_wave)
            if attempts_used < max_attempts:
                retry_delay = max(0.0, self.config.proxy_rotation_canary_retry_delay_seconds)
                recovery["ready_to_probe"] = True
                recovery["next_retry_at"] = time.monotonic() + retry_delay
                print(
                    f"Canary probe for proxy group {proxy_label} failed with a temporary startup error "
                    f"in slot {slot_id} ({user_id}): {fatal_error or status}; "
                    f"retrying same survivor in {retry_delay:.1f}s "
                    f"[attempt {attempts_used}/{max_attempts}]"
                )
            else:
                _schedule_proxy_group_recovery_retry(
                    proxy_label,
                    f"canary probe exhaustion ({fatal_error or status or 'startup failure'})",
                )
            return True

        def _drain_proxy_group_recoveries() -> None:
            if not self.config.proxy_rotation_recovery_enabled or not proxy_group_recoveries:
                return
            now = time.monotonic()
            for proxy_label in sorted(list(proxy_group_recoveries)):
                recovery = proxy_group_recoveries.get(proxy_label)
                if not recovery or now < float(recovery["next_retry_at"]):
                    continue
                if recovery.get("canary_slot_id") is not None and int(recovery["canary_slot_id"]) in workers:
                    continue
                if not _await_retired_profile_shutdown(proxy_label, recovery):
                    continue
                if bool(recovery.get("ready_to_probe")):
                    recovery["ready_to_probe"] = False
                    _start_group_canary_probe(proxy_label)
                    continue
                rotation_status = _attempt_rotate_proxy_group(proxy_label, str(recovery["proxy_value"]))
                if rotation_status == "failed":
                    _schedule_proxy_group_recovery_retry(proxy_label, "rotation trigger failure")
                    continue
                if rotation_status == "confirmed":
                    if not self.config.proxy_rotation_triage_survivors_on_ban:
                        print(
                            f"Proxy group {proxy_label} recovery wave confirmed on a new IP; "
                            "launching burn-all replacement backups without canary"
                        )
                        _activate_recovered_proxy_group(proxy_label)
                        continue
                    resume_delay = max(0.0, self.config.proxy_rotation_resume_delay_seconds)
                    recovery["next_retry_at"] = time.monotonic() + max(
                        0.0,
                        resume_delay,
                    )
                    recovery["backoff_seconds"] = max(
                        1.0,
                        self.config.proxy_rotation_recovery_initial_backoff_seconds,
                    )
                    recovery["ready_to_probe"] = True
                    recovery["canary_slot_id"] = None
                    recovery["canary_user_id"] = None
                    recovery["canary_attempts_used"] = 0
                    print(
                        f"Proxy group {proxy_label} recovery wave is ready for canary probing in "
                        f"{resume_delay:.1f}s"
                    )
                    continue
                if rotation_status == "inconclusive":
                    _schedule_proxy_group_recovery_retry(
                        proxy_label,
                        "rotation did not produce a verifiable exit IP change",
                    )

        initial_start_stagger = max(0.0, self.config.initial_slot_start_stagger_seconds)
        initial_start_jitter = max(0.0, self.config.initial_slot_start_jitter_seconds)

        for slot_id, user_id in enumerate(self.config.user_ids):
            _start_worker_for_slot(slot_id, user_id, is_backup=False)
            if slot_id < (len(self.config.user_ids) - 1):
                delay_seconds = initial_start_stagger
                if initial_start_jitter > 0:
                    delay_seconds += random.uniform(0.0, initial_start_jitter)
                if delay_seconds > 0:
                    print(
                        f"Initial startup stagger after slot {slot_id} ({user_id}): "
                        f"sleeping {delay_seconds:.1f}s before next slot"
                    )
                    time.sleep(delay_seconds)

        result_status = SessionResult(status="success", attempts=1)
        summary_written = False
        try:
            while (
                workers
                or pending_slot_resumes
                or pending_backup_launches
                or proxy_group_recoveries
                or self.backup_pool_manager.has_incomplete_batches()
            ):
                _drain_pending_slot_resumes()
                _drain_pending_backup_launches()
                _drain_proxy_group_recoveries()
                _reconcile_backup_capacity()

                if self.stop_event.is_set():
                    # Give workers a grace period to emit structured shutdown results.
                    grace_deadline = time.time() + 30
                    while time.time() < grace_deadline and (workers or proxy_group_recoveries):
                        try:
                            result = result_queue.get(timeout=0.5)
                        except queue.Empty:
                            result = None
                        except Exception as error:  # noqa: BLE001
                            if not _is_interrupt_error(error):
                                raise
                            self.stop_event.set()
                            result = None
                        if result is not None:
                            if result.get("kind") == "ban_signal":
                                _handle_ban_signal(result)
                                continue
                            if _should_ignore_stale_slot_event(int(result["slot_id"]), str(result["user_id"])):
                                print(
                                    f"Ignoring stale session result for slot {result['slot_id']} "
                                    f"({result['user_id']}); profile was already replaced or retired"
                                )
                                continue
                            slot_id = result["slot_id"]
                            self._mark_result(result)
                            self._update_profile_remark(result)
                            proc = workers.get(slot_id)
                            if proc is not None:
                                proc.join(timeout=2)
                            workers.pop(slot_id, None)
                            worker_user_ids.pop(slot_id, None)
                    _update_final_env_exports()
                    for proc in workers.values():
                        if proc.is_alive():
                            proc.terminate()
                    break

                try:
                    result = result_queue.get(timeout=0.5)
                except queue.Empty:
                    result = None
                except Exception as error:  # noqa: BLE001
                    if not _is_interrupt_error(error):
                        raise
                    self.stop_event.set()
                    result = None

                if result is not None:
                    if result.get("kind") == "ban_signal":
                        _handle_ban_signal(result)
                        continue
                    if _should_ignore_stale_slot_event(int(result["slot_id"]), str(result["user_id"])):
                        print(
                            f"Ignoring stale session result for slot {result['slot_id']} "
                            f"({result['user_id']}); profile was already replaced or retired"
                        )
                        continue
                    slot_id = result["slot_id"]
                    status = result["status"]
                    if status == "banned":
                        ban_pending_slots.add(slot_id)
                        ban_pending_users.add(str(result["user_id"]))
                    self._mark_result(result)
                    self._update_profile_remark(result)
                    completed_slots.add(slot_id)

                    proc = workers.get(slot_id)
                    if proc is not None:
                        proc.join(timeout=5)

                    workers.pop(slot_id, None)
                    worker_user_ids.pop(slot_id, None)
                    slot_dead_worker_counts[slot_id] = 0

                    if _handle_canary_probe_result(result):
                        continue

                    if status == "parked":
                        _launch_backup_for_slot(
                            slot_id,
                            str(result["user_id"]),
                            reason="parked shared infra collapse",
                        )
                        continue

                    if _is_shared_infra_collapse_result(result):
                        old_user_id = str(result["user_id"])
                        proxy_label = result.get("assigned_texting_proxy_label")
                        proxy_value = slot_proxy_values.get(slot_id)
                        if (
                            self.config.proxy_rotation_enabled
                            and self.config.proxy_rotation_recovery_enabled
                            and proxy_label
                            and proxy_value
                        ):
                            _queue_proxy_group_recovery_shared_collapse(
                                str(proxy_label),
                                proxy_value,
                                trigger_slot_id=slot_id,
                                trigger_user_id=old_user_id,
                                trigger_reason="shared infra collapse",
                            )
                            continue
                        _launch_backup_for_slot(
                            slot_id,
                            old_user_id,
                            reason="shared infra collapse",
                        )
                        continue

                    if status == "banned":
                        proxy_label = result.get("assigned_texting_proxy_label")
                        proxy_value = slot_proxy_values.get(slot_id)
                        if (
                            self.config.proxy_rotation_enabled
                            and self.config.proxy_rotation_recovery_enabled
                            and proxy_label
                            and proxy_value
                        ):
                            if not self.config.proxy_rotation_triage_survivors_on_ban:
                                _queue_proxy_group_recovery_burn_all(
                                    str(proxy_label),
                                    proxy_value,
                                    banned_slot_id=slot_id,
                                    banned_user_id=result["user_id"],
                                )
                                continue
                            if _add_banned_slot_to_recovery(
                                str(proxy_label),
                                slot_id,
                                str(result["user_id"]),
                                reason="ban",
                            ):
                                continue
                            quarantined_slots = _quarantine_proxy_group(proxy_label, slot_id)
                            triaged_banned_slots, resume_candidates = _triage_quarantined_slots_for_bans(
                                proxy_label,
                                quarantined_slots,
                            )
                            _queue_proxy_group_recovery(
                                proxy_label,
                                proxy_value,
                                banned_slot_id=slot_id,
                                banned_user_id=result["user_id"],
                                resume_candidates=resume_candidates,
                                triaged_banned_slots=triaged_banned_slots,
                            )
                            continue

                    if _should_launch_backup_for_result(slot_id, result):
                        old_user_id = result["user_id"]
                        if status == "banned":
                            _launch_backup_for_slot(slot_id, old_user_id, reason="ban")
                        elif str(result.get("fatal_error") or "") == "REGISTRATION_FAILED":
                            _launch_backup_for_slot(slot_id, old_user_id, reason="registration failure")
                        elif _is_post_rotation_match_error_result(result):
                            _launch_backup_for_slot(
                                slot_id,
                                old_user_id,
                                reason="persistent post-rotation API-abuse startup failure",
                            )
                        else:
                            _launch_backup_for_slot(
                                slot_id,
                                old_user_id,
                                reason="terminal pre-chat startup failure",
                            )

                # Handle unexpected hard exits with no reported result.
                dead_slots = [
                    slot_id
                    for slot_id, proc in workers.items()
                    if (not proc.is_alive()) and (slot_id not in completed_slots)
                ]
                for slot_id in dead_slots:
                    workers[slot_id].join(timeout=1)
                    old_user_id = worker_user_ids.get(slot_id)
                    slot_dead_worker_counts[slot_id] = slot_dead_worker_counts.get(slot_id, 0) + 1
                    workers.pop(slot_id, None)
                    worker_user_ids.pop(slot_id, None)
                    if slot_probe_modes.get(slot_id) == "canary_probe":
                        slot_probe_modes.pop(slot_id, None)
                        proxy_label, recovery = _find_recovery_for_canary_slot(slot_id)
                        if recovery and proxy_label is not None:
                            recovery["canary_slot_id"] = None
                            recovery["canary_user_id"] = None
                            attempts_used = int(recovery["canary_attempts_used"])
                            max_attempts = max(1, self.config.proxy_rotation_canary_attempts_per_wave)
                            if attempts_used < max_attempts:
                                retry_delay = max(0.0, self.config.proxy_rotation_canary_retry_delay_seconds)
                                recovery["ready_to_probe"] = True
                                recovery["next_retry_at"] = time.monotonic() + retry_delay
                                print(
                                    f"Canary probe worker for proxy group {proxy_label} exited without a result "
                                    f"for slot {slot_id} ({old_user_id or 'unknown'}); retrying in {retry_delay:.1f}s "
                                    f"[attempt {attempts_used}/{max_attempts}]"
                                )
                            else:
                                _schedule_proxy_group_recovery_retry(
                                    proxy_label,
                                    "canary probe worker exit without structured result",
                                )
                        continue
                    if not old_user_id:
                        continue
                    if self.stop_event.is_set():
                        continue
                    if slot_dead_worker_counts[slot_id] <= 1:
                        is_backup = bool(self.summary.get(old_user_id, {}).get("is_backup", False))
                        print(
                            f"Worker for slot {slot_id} ({old_user_id}) exited without a result; "
                            "restarting same profile once to preserve slot"
                        )
                        _start_worker_for_slot(
                            slot_id,
                            old_user_id,
                            is_backup=is_backup,
                            started_after_rotation_resume=slot_started_after_rotation_resume.get(slot_id, False),
                        )
                        completed_slots.discard(slot_id)
                    else:
                        _launch_backup_for_slot(
                            slot_id,
                            old_user_id,
                            reason="repeated worker exit without structured result",
                        )

                _reconcile_slot_liveness()

            for proc in workers.values():
                proc.join(timeout=5)

            _update_final_env_exports()

            if self.stop_event.is_set():
                result_status = SessionResult(status="stopped", attempts=1)
            else:
                result_status = SessionResult(status="success", attempts=1)

            self._write_summary()
            summary_written = True
            return result_status
        finally:
            self.stop_event.set()
            for proc in workers.values():
                if proc.is_alive():
                    proc.terminate()
            for proc in workers.values():
                proc.join(timeout=5)
            _update_final_env_exports()
            if not summary_written:
                self._write_summary()
            if self.backup_pool_manager is not None:
                self.backup_pool_manager.stop()
