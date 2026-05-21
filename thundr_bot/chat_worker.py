from __future__ import annotations

import logging
import random
import sys
import time
from pathlib import Path
from threading import Event
from typing import Callable

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from thundr_bot.adspower_client import AdsPowerClient
from thundr_bot.config import BotConfig, RuntimeContext, SessionResult


class StartupBanDetected(RuntimeError):
    """Raised when a startup-time Thundr ban page is detected."""


def _selector_locator(selector: str) -> tuple[str, str]:
    selector = selector.strip()
    if selector.lower().startswith("xpath="):
        return (By.XPATH, selector.split("=", 1)[1].strip())
    return (By.CSS_SELECTOR, selector)


def _ensure_active_thundr_tab(driver: webdriver.Chrome, url: str) -> None:
    target_host = "thundr.com"
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            current = (driver.current_url or "").lower()
            if target_host in current:
                driver.get(url)
                return
        except WebDriverException:
            continue

    try:
        driver.switch_to.new_window("tab")
    except WebDriverException:
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])
    driver.get(url)


def _normalize_browser_window(driver: webdriver.Chrome) -> None:
    target_left = 12
    target_top = 12
    width_margin = 36
    height_margin = 92
    screen_width = 1920
    screen_height = 1080

    try:
        screen_metrics = driver.execute_script(
            """
            return {
              width: Math.max(
                window.screen?.availWidth || 0,
                window.screen?.width || 0,
                0
              ),
              height: Math.max(
                window.screen?.availHeight || 0,
                window.screen?.height || 0,
                0
              ),
            };
            """
        )
        if isinstance(screen_metrics, dict):
            screen_width = max(1280, int(screen_metrics.get("width") or screen_width))
            screen_height = max(820, int(screen_metrics.get("height") or screen_height))
    except WebDriverException:
        pass

    target_width = max(1280, screen_width - width_margin)
    target_height = max(820, screen_height - height_margin)
    try:
        window_info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window_info.get("windowId")
        if window_id is not None:
            driver.execute_cdp_cmd(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            driver.execute_cdp_cmd(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "left": target_left,
                        "top": target_top,
                        "width": target_width,
                        "height": target_height,
                    },
                },
            )
            return
    except WebDriverException:
        pass

    try:
        driver.set_window_rect(
            x=target_left,
            y=target_top,
            width=target_width,
            height=target_height,
        )
    except WebDriverException:
        try:
            driver.set_window_size(target_width, target_height)
        except WebDriverException:
            return


def setup_logger(config: BotConfig, ctx: RuntimeContext) -> logging.Logger:
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger_name = f"thundr_{ctx.run_id}_{ctx.user_id}_{ctx.session_id}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | run=%(run_id)s | session=%(session_id)s | user=%(user_id)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(log_dir / f"{ctx.user_id}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger


def log_ctx(logger: logging.Logger, level: int, ctx: RuntimeContext, message: str) -> None:
    logger.log(
        level,
        message,
        extra={"run_id": ctx.run_id, "session_id": ctx.session_id, "user_id": ctx.user_id},
    )


def check_required_selectors(driver: webdriver.Chrome, config: BotConfig) -> None:
    wait = WebDriverWait(driver, config.element_wait_seconds)

    def _chat_surface_ready(_driver: webdriver.Chrome) -> bool:
        input_visible = _has_visible_element(_driver, config.chat_input_selector)
        state_visible = (
            _has_visible_element(_driver, config.active_chat_marker_selector)
            or is_looking_for_chat_state(_driver, config)
            or is_press_start_continue_state(_driver, config)
            or is_disconnected(_driver, config)
            or has_available_new_chat_button(_driver, config)
            or is_socket_error_active(_driver, config)
            or is_match_error_active(_driver, config)
        )
        return input_visible and state_visible and not is_probably_blank_chat_page(_driver, config)

    wait.until(_chat_surface_ready)


def ensure_chat_page_ready(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        check_required_selectors(driver, config)
        return True
    except TimeoutException:
        return False


def _wait_for_post_start_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    deadline = time.monotonic() + max(float(config.element_wait_seconds), 8.0)
    while time.monotonic() < deadline:
        if not _has_visible_element(driver, config.chat_input_selector):
            time.sleep(config.recovery_poll_seconds)
            continue
        if is_press_start_continue_state(driver, config):
            time.sleep(config.recovery_poll_seconds)
            continue
        if (
            _has_visible_element(driver, config.active_chat_marker_selector)
            or is_looking_for_chat_state(driver, config)
        ):
            return True
        time.sleep(config.recovery_poll_seconds)
    return False


def _log_chat_ready_diagnostics(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    label: str,
) -> None:
    try:
        current_url = _normalize_message_text(driver.current_url or "")
    except WebDriverException:
        current_url = "<unavailable>"

    try:
        title = _normalize_message_text(driver.title or "")
    except WebDriverException:
        title = "<unavailable>"

    try:
        body_text = _normalize_message_text(driver.find_element(By.TAG_NAME, "body").text)
    except WebDriverException:
        body_text = "<unavailable>"

    snippet = body_text[:220] if body_text and body_text != "<unavailable>" else body_text
    active_visible = _has_visible_element(driver, config.active_chat_marker_selector)
    input_visible = _has_visible_element(driver, config.chat_input_selector)
    send_visible = _has_visible_element(driver, config.send_button_selector)
    looking_visible = is_looking_for_chat_state(driver, config)
    press_start_visible = is_press_start_continue_state(driver, config)
    promo_visible = is_promo_popup_active(driver, config)
    loading_visible = is_loading_chat_transition(driver, config)
    socket_error_visible = is_socket_error_active(driver, config)
    disconnected_visible = is_disconnected(driver, config)
    match_error_visible = is_match_error_active(driver, config)
    blank_page = is_probably_blank_chat_page(driver, config)
    auth_chat_hybrid = _is_auth_chat_hybrid_state(driver, config)
    start_visible = _has_visible_element(driver, config.start_button_selector)
    layout_signals = _chat_shell_layout_signals(driver)
    malformed_shell = _is_malformed_chat_startup_shell(driver, config)

    log_ctx(
        logger,
        logging.WARNING,
        ctx,
        (
            f"{label} diagnostics: "
            f"url={current_url} | title={title or '<empty>'} | "
            f"active={active_visible} input={input_visible} send={send_visible} looking={looking_visible} "
            f"press_start={press_start_visible} promo={promo_visible} loading={loading_visible} "
            f"socket_error={socket_error_visible} "
            f"match_error={match_error_visible} "
            f"disconnected={disconnected_visible} blank={blank_page} hybrid={auth_chat_hybrid} "
            f"start={start_visible} malformed={malformed_shell} "
            f"scroll_ratio={layout_signals['scroll_ratio']:.2f} "
            f"stacked_landing={layout_signals['stacked_landing']} | "
            f"body_snippet={snippet or '<empty>'}"
        ),
    )


def is_looking_for_chat_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        selector = (config.looking_for_chat_selector or "").strip()
        return bool(selector and driver.find_elements(*_selector_locator(selector)))
    except WebDriverException:
        return False


def is_press_start_continue_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        selector = (config.press_start_continue_selector or "").strip()
        return bool(selector and driver.find_elements(*_selector_locator(selector)))
    except WebDriverException:
        return False


def is_probably_blank_chat_page(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        current_url = (driver.current_url or "").lower()
        if "thundr.com" not in current_url:
            return False

        body_text = _normalize_message_text(driver.find_element(By.TAG_NAME, "body").text)
        page_source = (driver.page_source or "").lower()

        has_expected_markers = any(
            marker in page_source
            for marker in (
                "thundr",
                "write a message",
                "match found",
                "looking for someone",
                "press start to begin chatting",
                "new chat",
                "start",
                "boost",
                "send",
            )
        )

        # A visible blank page typically has almost no rendered text and no meaningful app markers.
        return len(body_text) <= 10 and not has_expected_markers
    except WebDriverException:
        return False


def _is_auth_route(url: str) -> bool:
    return any(
        route in url
        for route in (
            "thundr.com/signin",
            "thundr.com/signup",
            "thundr.com/register",
        )
    )


def _auth_chat_hybrid_marker_summary(driver: webdriver.Chrome, config: BotConfig) -> dict[str, bool]:
    return {
        "start": _has_visible_element(driver, config.start_button_selector),
        "input": _has_visible_element(driver, config.chat_input_selector),
        "send": _has_visible_element(driver, config.send_button_selector),
        "messages": _has_visible_element(driver, config.message_container_selector),
    }


def _is_auth_chat_hybrid_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        current_url = (driver.current_url or "").lower()
    except WebDriverException:
        return False

    if not _is_auth_route(current_url):
        return False

    markers = _auth_chat_hybrid_marker_summary(driver, config)
    visible_marker_count = sum(1 for visible in markers.values() if visible)
    return visible_marker_count >= 2


def _chat_shell_layout_signals(driver: webdriver.Chrome) -> dict[str, float | bool]:
    try:
        metrics = driver.execute_script(
            """
            const body = document.body;
            const doc = document.documentElement;
            return {
              innerHeight: window.innerHeight || 0,
              scrollHeight: Math.max(
                body ? body.scrollHeight || 0 : 0,
                doc ? doc.scrollHeight || 0 : 0
              ),
              scrollY: window.scrollY || 0,
              bodyText: body ? (body.innerText || '') : '',
            };
            """
        )
    except WebDriverException:
        return {
            "inner_height": 0.0,
            "scroll_height": 0.0,
            "scroll_ratio": 0.0,
            "scroll_y": 0.0,
            "stacked_landing": False,
        }

    inner_height = float(metrics.get("innerHeight") or 0.0)
    scroll_height = float(metrics.get("scrollHeight") or 0.0)
    scroll_y = float(metrics.get("scrollY") or 0.0)
    body_text = _normalize_message_text(str(metrics.get("bodyText") or "")).lower()
    scroll_ratio = (scroll_height / inner_height) if inner_height > 0 else 0.0
    stacked_landing = "vibe with beautiful people" in body_text or "beautiful people" in body_text

    return {
        "inner_height": inner_height,
        "scroll_height": scroll_height,
        "scroll_ratio": scroll_ratio,
        "scroll_y": scroll_y,
        "stacked_landing": stacked_landing,
    }


def _is_malformed_chat_startup_shell(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        input_visible = _has_visible_element(driver, config.chat_input_selector)
        send_visible = _has_visible_element(driver, config.send_button_selector)
        start_visible = _has_visible_element(driver, config.start_button_selector)
        if not (input_visible and send_visible and start_visible):
            return False

        if (
            _is_confirmed_active_chat(driver, config)
            or is_looking_for_chat_state(driver, config)
            or is_disconnected(driver, config)
            or has_available_new_chat_button(driver, config)
            or is_socket_error_active(driver, config)
            or is_match_error_active(driver, config)
            or is_loading_chat_transition(driver, config)
        ):
            return False

        layout_signals = _chat_shell_layout_signals(driver)
        if bool(layout_signals["stacked_landing"]):
            return True
        return float(layout_signals["scroll_ratio"]) >= 1.45
    except WebDriverException:
        return False


def _can_sample_inbound_transcript(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        return _is_confirmed_active_chat(driver, config)
    except WebDriverException:
        return False


def process_incoming_messages(
    driver: webdriver.Chrome,
    state: dict[str, int],
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
) -> dict[str, int]:
    if not _can_sample_inbound_transcript(driver, config):
        return state
    messages = incoming_messages(driver, config)
    current_count = len(messages)

    if current_count < state["last_message_count"]:
        state["last_message_count"] = current_count

    if current_count > state["last_message_count"]:
        latest_message = _message_preview(messages[-1] if messages else "")
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"Incoming message count increased: {current_count} | latest_incoming={latest_message!r}",
        )
        state["last_message_count"] = current_count

    return state


def incoming_message_count(driver: webdriver.Chrome, config: BotConfig) -> int:
    if not _can_sample_inbound_transcript(driver, config):
        return 0
    return len(incoming_messages(driver, config))


def _normalize_message_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _starts_with_flag_country_badge(text: str) -> bool:
    normalized = _normalize_message_text(text)
    if len(normalized) < 2:
        return False
    prefix = normalized[:2]
    if not all(0x1F1E6 <= ord(char) <= 0x1F1FF for char in prefix):
        return False
    remainder = normalized[2:].strip()
    if not remainder:
        return True
    return all(char.isalpha() or char in {" ", "-", "'", "."} for char in remainder)


def _is_transient_inbound_text(text: str) -> bool:
    raw_normalized = _normalize_message_text(text)
    normalized = raw_normalized.lower().replace("…", "...")
    if not normalized:
        return True
    transient_markers = (
        "stranger is typing",
        "typing",
    )
    if any(normalized.startswith(marker) for marker in transient_markers):
        return True

    if _starts_with_flag_country_badge(raw_normalized):
        return True

    metadata_markers = {
        "private",
        "unknown",
        "👻 private",
        "🏴‍☠️ unknown",
    }
    return normalized in metadata_markers


def incoming_messages(driver: webdriver.Chrome, config: BotConfig) -> list[str]:
    if not _can_sample_inbound_transcript(driver, config):
        return []
    try:
        elements = driver.find_elements(*_selector_locator(config.incoming_message_selector))
    except WebDriverException:
        return []

    messages: list[str] = []
    for element in elements:
        try:
            text = _normalize_message_text(element.text)
        except WebDriverException:
            continue
        if not text:
            continue
        if text.startswith("Stranger:"):
            text = _normalize_message_text(text.removeprefix("Stranger:"))
        if _is_transient_inbound_text(text):
            continue
        messages.append(text)
    return messages


def latest_incoming_message(driver: webdriver.Chrome, config: BotConfig) -> str:
    messages = incoming_messages(driver, config)
    return messages[-1] if messages else ""


def _log_inbound_snapshot(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    label: str,
    state: dict[str, int] | None = None,
) -> None:
    try:
        current_count = incoming_message_count(driver, config)
        latest_message = _message_preview(latest_incoming_message(driver, config))
    except WebDriverException as error:
        log_ctx(logger, logging.WARNING, ctx, f"{label}: inbound snapshot failed: {error}")
        return

    state_bits: list[str] = []
    if state is not None:
        state_bits.extend(
            [
                f"state.last_message_count={state.get('last_message_count')}",
                f"state.awaiting_reply={state.get('awaiting_reply')}",
                f"state.inbound_count_at_last_outbound={state.get('inbound_count_at_last_outbound')}",
                f"state.next_response_index={state.get('next_response_index')}",
            ]
        )
    state_suffix = f" | {' '.join(state_bits)}" if state_bits else ""
    log_ctx(
        logger,
        logging.INFO,
        ctx,
        (
            f"{label}: inbound snapshot | live_incoming_count={current_count} "
            f"latest_incoming={latest_message!r}{state_suffix}"
        ),
    )


def _sync_state_inbound_baseline(
    driver: webdriver.Chrome,
    config: BotConfig,
    state: dict[str, int],
) -> int:
    baseline_count = incoming_message_count(driver, config)
    state["last_message_count"] = baseline_count
    state["inbound_count_at_last_outbound"] = baseline_count
    return baseline_count


def outgoing_messages(driver: webdriver.Chrome, config: BotConfig) -> list[str]:
    try:
        elements = driver.find_elements(*_selector_locator(config.outgoing_message_selector))
    except WebDriverException:
        return []

    messages: list[str] = []
    for element in elements:
        try:
            text = _normalize_message_text(element.text)
        except WebDriverException:
            continue
        if not text:
            continue
        if text.startswith("You:"):
            text = _normalize_message_text(text.removeprefix("You:"))
        messages.append(text)
    return messages


def _chat_transcript_signature(
    driver: webdriver.Chrome, config: BotConfig
) -> tuple[int, int, str, str]:
    incoming = incoming_messages(driver, config)
    outgoing = outgoing_messages(driver, config)
    latest_incoming = incoming[-1] if incoming else ""
    latest_outgoing = outgoing[-1] if outgoing else ""
    return (len(incoming), len(outgoing), latest_incoming, latest_outgoing)


def has_outgoing_message(driver: webdriver.Chrome, config: BotConfig, message: str) -> bool:
    target = _normalize_message_text(message)
    if not target:
        return False
    return any(existing == target for existing in outgoing_messages(driver, config))


def _is_confirmed_active_chat(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return (
        _has_visible_element(driver, config.chat_input_selector)
        and _has_visible_element(driver, config.active_chat_marker_selector)
        and not is_looking_for_chat_state(driver, config)
        and not is_press_start_continue_state(driver, config)
        and not is_disconnected(driver, config)
        and not is_loading_chat_transition(driver, config)
        and not is_socket_error_active(driver, config)
        and not is_match_error_active(driver, config)
    )


def _wait_for_confirmed_active_chat(driver: webdriver.Chrome, config: BotConfig, timeout_seconds: float | None = None) -> None:
    timeout = max(timeout_seconds or float(config.element_wait_seconds), 2.0)
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda current: _is_confirmed_active_chat(current, config))


def _wait_for_stable_active_chat_window(
    driver: webdriver.Chrome,
    config: BotConfig,
    *,
    timeout_seconds: float = 3.0,
    stable_window_seconds: float | None = None,
) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    stable_window = stable_window_seconds or max(
        0.75,
        float(getattr(config, "recovery_poll_seconds", 0.25)) * 4.0,
    )
    stable_started_ts: float | None = None

    while time.monotonic() < deadline:
        if _is_confirmed_active_chat(driver, config):
            if stable_started_ts is None:
                stable_started_ts = time.monotonic()
            elif (time.monotonic() - stable_started_ts) >= stable_window:
                return True
        else:
            stable_started_ts = None
        time.sleep(min(0.1, max(0.02, float(getattr(config, "recovery_poll_seconds", 0.25)))))

    return False


def _message_preview(message: str, limit: int = 60) -> str:
    normalized = _normalize_message_text(message)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _read_chat_input_value(element) -> str:
    for attribute_name in ("value", "innerHTML", "textContent"):
        try:
            value = element.get_attribute(attribute_name)
        except WebDriverException:
            continue
        if value:
            return _normalize_message_text(value)
    try:
        return _normalize_message_text(element.text)
    except WebDriverException:
        return ""


def _clear_chat_input_if_present(driver: webdriver.Chrome, config: BotConfig) -> None:
    try:
        input_field = driver.find_element(*_selector_locator(config.chat_input_selector))
    except WebDriverException:
        return
    try:
        input_field.click()
    except WebDriverException:
        pass
    try:
        input_field.send_keys(Keys.CONTROL, "a")
        input_field.send_keys(Keys.DELETE)
    except WebDriverException:
        pass


def _send_interrupted_state_reason(driver: webdriver.Chrome, config: BotConfig) -> str | None:
    if is_disconnected(driver, config):
        return "disconnect"
    if is_press_start_continue_state(driver, config):
        return "press-start"
    if is_looking_for_chat_state(driver, config):
        return "looking"
    if is_loading_chat_transition(driver, config):
        return "loading"
    if is_socket_error_active(driver, config):
        return "socket-error"
    if is_match_error_active(driver, config):
        return "match-error"
    return None


def _wait_for_send_confirmation(
    driver: webdriver.Chrome,
    config: BotConfig,
    message: str,
    *,
    prior_outgoing_count: int,
    timeout_seconds: float | None = None,
) -> None:
    timeout = max(timeout_seconds or float(config.element_wait_seconds), 2.0)
    target_message = _normalize_message_text(message)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        interrupted_reason = _send_interrupted_state_reason(driver, config)
        if interrupted_reason is not None:
            _clear_chat_input_if_present(driver, config)
            raise WebDriverException(
                f"Send confirmation aborted because chat entered {interrupted_reason} state"
            )
        if has_outgoing_message(driver, config, target_message):
            return
        current_outgoing = len(outgoing_messages(driver, config))
        if current_outgoing > prior_outgoing_count:
            return
        try:
            input_field = driver.find_element(*_selector_locator(config.chat_input_selector))
            if not _read_chat_input_value(input_field):
                return
        except WebDriverException:
            pass
        time.sleep(0.2)

    raise TimeoutException("Timed out waiting for message send confirmation")


def send_chat_message(
    driver: webdriver.Chrome,
    config: BotConfig,
    message: str,
    *,
    delay_min_seconds: float | None = None,
    delay_max_seconds: float | None = None,
    logger: logging.Logger | None = None,
    ctx: RuntimeContext | None = None,
    send_label: str = "chat-message",
) -> float:
    _wait_for_confirmed_active_chat(driver, config)
    min_delay = config.reply_delay_min_seconds if delay_min_seconds is None else delay_min_seconds
    max_delay = config.reply_delay_max_seconds if delay_max_seconds is None else delay_max_seconds
    send_delay = random.uniform(
        min(min_delay, max_delay),
        max(min_delay, max_delay),
    )
    if logger is not None and ctx is not None:
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"{send_label}: preparing send after {send_delay:.2f}s delay | text={_message_preview(message)!r}",
        )
    delay_deadline = time.monotonic() + send_delay
    while time.monotonic() < delay_deadline:
        interrupted_reason = _send_interrupted_state_reason(driver, config)
        if interrupted_reason is not None:
            raise WebDriverException(
                f"{send_label} aborted before send because chat entered {interrupted_reason} state"
            )
        time.sleep(min(0.1, max(0.02, delay_deadline - time.monotonic())))
    wait = WebDriverWait(driver, config.element_wait_seconds)
    _wait_for_confirmed_active_chat(driver, config)
    prior_outgoing_count = len(outgoing_messages(driver, config))
    input_field = wait.until(EC.presence_of_element_located(_selector_locator(config.chat_input_selector)))
    try:
        input_field.click()
    except WebDriverException:
        pass
    try:
        input_field.send_keys(Keys.CONTROL, "a")
        input_field.send_keys(Keys.DELETE)
    except WebDriverException:
        pass
    input_field.send_keys(message)
    interrupted_reason = _send_interrupted_state_reason(driver, config)
    if interrupted_reason is not None:
        _clear_chat_input_if_present(driver, config)
        raise WebDriverException(
            f"{send_label} aborted after typing because chat entered {interrupted_reason} state"
        )
    submit_wait_seconds = min(
        1.0,
        max(0.5, float(getattr(config, "recovery_poll_seconds", 0.25)) * 2.0),
    )
    try:
        send_button = WebDriverWait(driver, submit_wait_seconds).until(
            EC.element_to_be_clickable(_selector_locator(config.send_button_selector))
        )
        if not _click_element_with_fallbacks(driver, send_button, retries=3):
            input_field.send_keys(Keys.ENTER)
    except TimeoutException:
        input_field.send_keys(Keys.ENTER)
    try:
        _wait_for_send_confirmation(
            driver,
            config,
            message,
            prior_outgoing_count=prior_outgoing_count,
        )
    except TimeoutException:
        retry_value = _read_chat_input_value(input_field)
        if retry_value and _normalize_message_text(message) in retry_value:
            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                logging.WARNING,
                ctx,
                f"{send_label}: message still pending in textbox; retrying submit once",
            )
            try:
                send_button = WebDriverWait(driver, submit_wait_seconds).until(
                    EC.element_to_be_clickable(_selector_locator(config.send_button_selector))
                )
                if not _click_element_with_fallbacks(driver, send_button, retries=3):
                    input_field.send_keys(Keys.ENTER)
            except TimeoutException:
                input_field.send_keys(Keys.ENTER)
            _wait_for_send_confirmation(
                driver,
                config,
                message,
                prior_outgoing_count=prior_outgoing_count,
                timeout_seconds=max(float(config.element_wait_seconds), 2.0),
            )
        else:
            raise
    confirmed_at = time.monotonic()
    if logger is not None and ctx is not None:
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            (
                f"{send_label}: send confirmed | outgoing_before={prior_outgoing_count} "
                f"outgoing_after={len(outgoing_messages(driver, config))}"
            ),
        )
    return confirmed_at


def send_opener_message(
    driver: webdriver.Chrome,
    config: BotConfig,
    message: str,
    *,
    logger: logging.Logger | None = None,
    ctx: RuntimeContext | None = None,
    send_label: str = "opener",
) -> float:
    return send_chat_message(
        driver,
        config,
        message,
        delay_min_seconds=config.opener_delay_min_seconds,
        delay_max_seconds=config.opener_delay_max_seconds,
        logger=logger,
        ctx=ctx,
        send_label=send_label,
    )


def _opener_parts(config: BotConfig, opener: str) -> list[str]:
    normalized_opener = _normalize_message_text(opener)
    if not normalized_opener:
        return []

    delimiter = (config.split_opener_delimiter or "").strip()
    if (
        not config.split_opener_enabled
        or not delimiter
        or delimiter not in opener
    ):
        return [normalized_opener]

    first, second = opener.split(delimiter, 1)
    first = _normalize_message_text(first)
    second = _normalize_message_text(second)
    if not first or not second:
        return [normalized_opener]
    return [first, second]


def _send_configured_opener(
    driver: webdriver.Chrome,
    config: BotConfig,
    opener: str,
    *,
    logger: logging.Logger | None = None,
    ctx: RuntimeContext | None = None,
    send_label: str = "opener",
) -> float | None:
    parts = _opener_parts(config, opener)
    if not parts:
        return None

    if logger is not None and ctx is not None:
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"{send_label}: sending opener with {len(parts)} part(s)",
        )
    confirmed_at = send_opener_message(
        driver,
        config,
        parts[0],
        logger=logger,
        ctx=ctx,
        send_label=f"{send_label} part 1/{len(parts)}",
    )
    if len(parts) == 1:
        return confirmed_at

    return send_chat_message(
        driver,
        config,
        parts[1],
        delay_min_seconds=config.split_opener_delay_min_seconds,
        delay_max_seconds=config.split_opener_delay_max_seconds,
        logger=logger,
        ctx=ctx,
        send_label=f"{send_label} part 2/{len(parts)}",
    )


def _has_any_opener_part_outgoing(driver: webdriver.Chrome, config: BotConfig, opener: str) -> bool:
    return any(has_outgoing_message(driver, config, part) for part in _opener_parts(config, opener))


def _next_reply_due_ts(config: BotConfig) -> float:
    return time.monotonic() + random.uniform(
        min(config.reply_delay_min_seconds, config.reply_delay_max_seconds),
        max(config.reply_delay_min_seconds, config.reply_delay_max_seconds),
    )


def _click_element_with_fallbacks(driver: webdriver.Chrome, element, retries: int = 2) -> bool:
    for _ in range(max(1, retries)):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        except WebDriverException:
            pass

        try:
            if not (element.is_displayed() and element.is_enabled()):
                time.sleep(0.25)
                continue
        except WebDriverException:
            time.sleep(0.25)
            continue

        try:
            element.click()
            return True
        except WebDriverException:
            pass

        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except WebDriverException:
            time.sleep(0.25)

    return False


def _has_visible_element(driver: webdriver.Chrome, selector: str) -> bool:
    if not selector.strip():
        return False
    try:
        for element in driver.find_elements(*_selector_locator(selector)):
            try:
                if element.is_displayed():
                    return True
            except WebDriverException:
                continue
    except WebDriverException:
        return False
    return False


def _click_confirm_if_present(driver: webdriver.Chrome, selector: str, timeout_seconds: int) -> bool:
    if not selector.strip():
        return False
    try:
        wait = WebDriverWait(driver, timeout_seconds)
        button = wait.until(EC.element_to_be_clickable(_selector_locator(selector)))
        return _click_element_with_fallbacks(driver, button)
    except TimeoutException:
        return False


def _js_click_new_chat_control(driver: webdriver.Chrome) -> bool:
    script = """
const labels = new Set(["start", "next", "new chat"]);
const candidates = Array.from(document.querySelectorAll("button, [role='button']"));
for (const candidate of candidates) {
  const text = (candidate.innerText || candidate.textContent || "").replace(/\\s+/g, " ").trim().toLowerCase();
  if (!labels.has(text)) continue;
  const style = window.getComputedStyle(candidate);
  if (style.visibility === "hidden" || style.display === "none") continue;
  if (candidate.disabled) continue;
  if (!candidate.getClientRects().length) continue;
  candidate.click();
  return text;
}
return "";
"""
    try:
        return bool(driver.execute_script(script))
    except WebDriverException:
        return False


def click_new_chat(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger | None = None,
    ctx: RuntimeContext | None = None,
    action_label: str = "new-chat",
) -> bool:
    for selector in (config.new_chat_selector, config.start_button_selector):
        locator = _selector_locator(selector)
        try:
            candidates = driver.find_elements(*locator)
        except WebDriverException:
            candidates = []

        for button in candidates:
            try:
                if not (button.is_displayed() and button.is_enabled()):
                    continue
                if not _click_element_with_fallbacks(driver, button, retries=3):
                    if logger is not None and ctx is not None:
                        log_ctx(
                            logger,
                            logging.WARNING,
                            ctx,
                            f"{action_label}: found visible New/Start control but it was not interactable",
                        )
                    continue
                _click_confirm_if_present(driver, config.new_chat_confirm_selector, timeout_seconds=2)
                return True
            except WebDriverException:
                continue
    if _js_click_new_chat_control(driver):
        _click_confirm_if_present(driver, config.new_chat_confirm_selector, timeout_seconds=2)
        return True
    return False


def has_available_new_chat_button(driver: webdriver.Chrome, config: BotConfig) -> bool:
    for selector in (config.new_chat_selector, config.start_button_selector):
        try:
            elements = driver.find_elements(*_selector_locator(selector))
        except WebDriverException:
            continue

        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return True
            except WebDriverException:
                continue
    return False


def _has_exact_disconnect_new_chat_button(driver: webdriver.Chrome) -> bool:
    try:
        elements = driver.find_elements(
            By.XPATH,
            "//button[normalize-space()='New Chat']",
        )
    except WebDriverException:
        return False

    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                return True
        except WebDriverException:
            continue
    return False


def _has_disconnect_turn_on_video_button(driver: webdriver.Chrome) -> bool:
    try:
        elements = driver.find_elements(
            By.XPATH,
            "//button[normalize-space()='turn on video']",
        )
    except WebDriverException:
        return False

    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                return True
        except WebDriverException:
            continue
    return False


def is_loading_chat_transition(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        loading_selector = (config.loading_chat_selector or "").strip()
        if loading_selector and driver.find_elements(*_selector_locator(loading_selector)):
            return True
        source = driver.page_source.lower()
        return "loading" in source and "swipe" in source and "esc" in source
    except WebDriverException:
        return False


def is_promo_popup_active(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        selector = (config.promo_dialog_selector or "").strip()
        if selector and driver.find_elements(*_selector_locator(selector)):
            return True
        source = (driver.page_source or "").lower()
        return "buy a boost!" in source and "match *only* with the hottest people on thundr!" in source
    except WebDriverException:
        return False


def dismiss_promo_popup(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger | None = None,
    ctx: RuntimeContext | None = None,
    action_label: str = "promo-dismiss",
) -> bool:
    if not is_promo_popup_active(driver, config):
        return False

    wait_started = False
    deadline = time.monotonic() + max(config.promo_skip_wait_seconds + 2.0, 3.0)
    while time.monotonic() < deadline:
        if not wait_started:
            time.sleep(max(0.0, config.promo_skip_wait_seconds))
            wait_started = True

        # Prefer the human-like path ("Skip"), then fallback to the same selector again if needed.
        for selector in (config.promo_maybe_later_selector, config.promo_close_selector):
            if not selector.strip():
                continue
            try:
                elements = driver.find_elements(*_selector_locator(selector))
                if not elements:
                    continue
                saw_visible_candidate = False
                for element in elements:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            saw_visible_candidate = True
                            if _click_element_with_fallbacks(driver, element, retries=3):
                                return True
                    except WebDriverException:
                        continue
                if saw_visible_candidate and logger is not None and ctx is not None:
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        f"{action_label}: promo control was visible but not interactable",
                    )
            except WebDriverException:
                continue
        time.sleep(0.5)
    return False


def _attempt_rotate_transition(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    action_label: str,
) -> bool:
    try:
        if dismiss_promo_popup(
            driver,
            config,
            logger,
            ctx,
            f"{action_label} pre-rotate promo dismiss",
        ):
            log_ctx(logger, logging.INFO, ctx, f"Promo popup dismissed before {action_label}")
    except WebDriverException as error:
        log_ctx(
            logger,
            logging.WARNING,
            ctx,
            f"{action_label}: promo-dismiss step failed before rotate: {error}",
        )
        return False

    deadline = time.monotonic() + max(config.rotate_click_grace_seconds, config.recovery_poll_seconds)
    clicked = False
    while time.monotonic() < deadline:
        try:
            clicked = click_new_chat(driver, config, logger, ctx, action_label)
        except WebDriverException as error:
            log_ctx(
                logger,
                logging.WARNING,
                ctx,
                f"{action_label}: New/Start click step failed: {error}",
            )
            return False
        if clicked:
            break
        time.sleep(config.recovery_poll_seconds)

    if not clicked:
        return False

    try:
        if dismiss_promo_popup(
            driver,
            config,
            logger,
            ctx,
            f"{action_label} post-click promo dismiss",
        ):
            log_ctx(logger, logging.INFO, ctx, f"Promo popup dismissed after {action_label}")
    except WebDriverException as error:
        log_ctx(
            logger,
            logging.WARNING,
            ctx,
            f"{action_label}: promo-dismiss step failed after New/Start click: {error}",
        )
        return False

    return True


def _send_opener_with_rotate_guard(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    action_label: str,
    message: str,
) -> float | None:
    try:
        return _send_configured_opener(
            driver,
            config,
            message,
            logger=logger,
            ctx=ctx,
            send_label=f"{action_label} opener",
        )
    except WebDriverException as error:
        log_ctx(
            logger,
            logging.WARNING,
            ctx,
            f"{action_label}: opener send failed after rotate: {error}",
        )
        return None


def _wait_for_post_rotate_chat_ready(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    action_label: str,
    opener: str,
    timeout_seconds: float | None = None,
    prior_transcript_signature: tuple[int, int, str, str] | None = None,
    allow_matchmaking_rearm: bool = False,
) -> bool:
    deadline = time.monotonic() + max(
        timeout_seconds or float(config.element_wait_seconds),
        4.0,
    )
    saw_stale_transcript = False
    stable_active_started_ts: float | None = None
    stable_window_seconds = max(0.75, float(getattr(config, "recovery_poll_seconds", 0.25)) * 4.0)
    promo_retry_budget = max(1, int(getattr(config, "post_rotate_ui_retry_attempts", 2)))
    start_retry_budget = promo_retry_budget
    disconnect_click_retry_budget = max(2, promo_retry_budget + 1)
    promo_stall_attempts = 0
    start_blocked_attempts = 0
    disconnect_click_attempts = 0
    saw_matchmaking_rearm = False

    while time.monotonic() < deadline:
        if is_promo_popup_active(driver, config):
            if dismiss_promo_popup(
                driver,
                config,
                logger,
                ctx,
                f"{action_label} post-rotate promo dismiss",
            ):
                promo_stall_attempts = 0
                log_ctx(logger, logging.INFO, ctx, f"Promo popup dismissed during {action_label} readiness wait")
                time.sleep(config.recovery_poll_seconds)
                continue
            promo_stall_attempts += 1
            if promo_stall_attempts <= promo_retry_budget:
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        f"{action_label}: promo popup still active during post-rotate readiness wait "
                        f"({promo_stall_attempts}/{promo_retry_budget}); retrying in place"
                    ),
                )
                time.sleep(config.recovery_poll_seconds)
                continue
            log_ctx(
                logger,
                logging.WARNING,
                ctx,
                f"{action_label}: promo popup stayed active during post-rotate readiness wait",
            )
            return False

        if is_press_start_continue_state(driver, config):
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                f"{action_label}: post-rotate state requires Start; clicking to resume matchmaking",
            )
            if not click_new_chat(driver, config, logger, ctx, f"{action_label} post-rotate press-start"):
                start_blocked_attempts += 1
                if start_blocked_attempts <= start_retry_budget:
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        (
                            f"{action_label}: post-rotate Start control stayed blocked "
                            f"({start_blocked_attempts}/{start_retry_budget}); retrying in place"
                        ),
                    )
                    time.sleep(config.recovery_poll_seconds)
                    continue
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    f"{action_label}: post-rotate Start control stayed blocked",
                )
                return False
            start_blocked_attempts = 0
            time.sleep(config.recovery_poll_seconds)
            if allow_matchmaking_rearm and is_looking_for_chat_state(driver, config) and not saw_matchmaking_rearm:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"{action_label}: matchmaking re-armed after rotate; awaiting active match",
                )
                saw_matchmaking_rearm = True
            continue

        if is_disconnected(driver, config):
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                f"{action_label}: post-rotate state is still disconnected; clicking New Chat/Start to continue",
            )
            if not click_new_chat(driver, config, logger, ctx, f"{action_label} post-rotate disconnected"):
                disconnect_click_attempts += 1
                if disconnect_click_attempts <= disconnect_click_retry_budget:
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        (
                            f"{action_label}: disconnected recovery control stayed blocked "
                            f"({disconnect_click_attempts}/{disconnect_click_retry_budget}); retrying in place"
                        ),
                    )
                    time.sleep(config.recovery_poll_seconds)
                    continue
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    f"{action_label}: disconnected recovery control stayed blocked",
                )
                return False
            disconnect_click_attempts = 0
            stable_active_started_ts = None
            time.sleep(config.recovery_poll_seconds)
            if allow_matchmaking_rearm and is_looking_for_chat_state(driver, config) and not saw_matchmaking_rearm:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"{action_label}: matchmaking re-armed after disconnected state; awaiting active match",
                )
                saw_matchmaking_rearm = True
            continue

        if is_looking_for_chat_state(driver, config):
            stable_active_started_ts = None
            if allow_matchmaking_rearm and not saw_matchmaking_rearm:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"{action_label}: matchmaking is active after rotate; awaiting active match",
                )
                saw_matchmaking_rearm = True
            time.sleep(config.recovery_poll_seconds)
            continue

        active_chat_ready = (
            _has_visible_element(driver, config.active_chat_marker_selector)
            and _has_visible_element(driver, config.chat_input_selector)
            and not is_disconnected(driver, config)
            and not is_loading_chat_transition(driver, config)
            and not is_socket_error_active(driver, config)
            and not is_match_error_active(driver, config)
        )
        if active_chat_ready:
            current_signature = (
                _chat_transcript_signature(driver, config)
                if prior_transcript_signature is not None
                else None
            )
            if (
                prior_transcript_signature is not None
                and current_signature == prior_transcript_signature
                and (current_signature[0] > 0 or current_signature[1] > 0)
            ):
                if not saw_stale_transcript:
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        f"{action_label}: transcript fingerprint is unchanged after rotate; waiting for a fresh chat state",
                    )
                    saw_stale_transcript = True
                stable_active_started_ts = None
                time.sleep(config.recovery_poll_seconds)
                continue
            if _has_any_opener_part_outgoing(driver, config, opener):
                if not saw_stale_transcript:
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        f"{action_label}: old opener transcript is still present after rotate; waiting for fresh chat state",
                    )
                    saw_stale_transcript = True
                stable_active_started_ts = None
                time.sleep(config.recovery_poll_seconds)
                continue
            if stable_active_started_ts is None:
                stable_active_started_ts = time.monotonic()
            elif (time.monotonic() - stable_active_started_ts) >= stable_window_seconds:
                return True
        else:
            stable_active_started_ts = None

        time.sleep(config.recovery_poll_seconds)

    if saw_stale_transcript:
        log_ctx(
            logger,
            logging.WARNING,
            ctx,
            f"{action_label}: timed out waiting for a confirmed fresh transcript after rotate",
        )
    else:
        log_ctx(
            logger,
            logging.WARNING,
            ctx,
            f"{action_label}: timed out waiting for confirmed active chat after rotate",
        )
    return False


def _stabilize_rotate_recovery(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    action_label: str,
    opener: str,
    ready_timeout_seconds: float,
    local_attempts: int,
    prior_transcript_signature: tuple[int, int, str, str] | None = None,
    allow_matchmaking_rearm: bool = False,
) -> bool:
    attempts = max(1, local_attempts)
    retry_pause_seconds = max(0.5, float(getattr(config, "recovery_poll_seconds", 0.25)) * 2.0)

    for attempt_index in range(1, attempts + 1):
        current_label = (
            action_label
            if attempts == 1
            else f"{action_label} attempt {attempt_index}/{attempts}"
        )
        if attempt_index > 1:
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                (
                    f"{action_label}: retrying in place without refresh "
                    f"({attempt_index}/{attempts})"
                ),
            )

        if not _attempt_rotate_transition(
            driver,
            config,
            logger,
            ctx,
            action_label=current_label,
        ):
            if attempt_index < attempts:
                time.sleep(retry_pause_seconds)
                continue
            return False

        if _wait_for_post_rotate_chat_ready(
            driver,
            config,
            logger,
            ctx,
            action_label=current_label,
            opener=opener,
            timeout_seconds=ready_timeout_seconds,
            prior_transcript_signature=prior_transcript_signature,
            allow_matchmaking_rearm=allow_matchmaking_rearm,
        ):
            return True

        if attempt_index < attempts:
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                (
                    f"{action_label}: rotate attempt {attempt_index}/{attempts} "
                    "did not stabilize yet; retrying without refresh"
                ),
            )
            time.sleep(retry_pause_seconds)

    return False


def is_disconnected(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        disconnect_selector = (config.disconnect_selector or "").strip()
        disconnect_text_visible = False
        if disconnect_selector:
            disconnect_locator = _selector_locator(disconnect_selector)
            for element in driver.find_elements(*disconnect_locator):
                try:
                    if element.is_displayed():
                        disconnect_text_visible = True
                        break
                except WebDriverException:
                    continue
        if disconnect_text_visible:
            return True
        if _has_exact_disconnect_new_chat_button(driver):
            return True
        if _has_disconnect_turn_on_video_button(driver):
            return True
        source = (driver.page_source or "").lower()
        return "stranger disconnected." in source or "you have disconnected." in source
    except WebDriverException:
        return False


def is_banned(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        banned_selector = (config.banned_selector or "").strip()
        if banned_selector and driver.find_elements(*_selector_locator(banned_selector)):
            return True
        return "you have been banned" in driver.page_source.lower()
    except WebDriverException:
        return False


def is_swipe_gate_active(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        swipe_selector = (config.swipe_gate_selector or "").strip()
        if swipe_selector and driver.find_elements(*_selector_locator(swipe_selector)):
            return True
        source = driver.page_source.lower()
        return "swipe up to start a new chat" in source
    except WebDriverException:
        return False


def is_socket_error_active(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        socket_error_selector = (config.socket_error_selector or "").strip()
        if socket_error_selector and driver.find_elements(*_selector_locator(socket_error_selector)):
            return True
        source = driver.page_source.lower()
        return "error connecting to socket" in source or "error connecting to server" in source
    except WebDriverException:
        return False


def is_connected_recovery_state(driver: webdriver.Chrome) -> bool:
    try:
        body_text = _normalize_message_text(driver.find_element(By.TAG_NAME, "body").text).lower()
        return "connected" in body_text
    except WebDriverException:
        return False


def _is_socket_recovery_actionable(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        return (
            is_connected_recovery_state(driver)
            or is_disconnected(driver, config)
            or is_press_start_continue_state(driver, config)
            or has_available_new_chat_button(driver, config)
        )
    except WebDriverException:
        return False


def _use_fast_loop_poll(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        return (
            _is_confirmed_active_chat(driver, config)
            or is_press_start_continue_state(driver, config)
            or is_looking_for_chat_state(driver, config)
            or is_loading_chat_transition(driver, config)
            or is_socket_error_active(driver, config)
            or is_match_error_active(driver, config)
            or is_disconnected(driver, config)
            or has_available_new_chat_button(driver, config)
        )
    except WebDriverException:
        return False


def is_match_error_active(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        match_error_selector = (config.match_error_selector or "").strip()
        if match_error_selector and driver.find_elements(*_selector_locator(match_error_selector)):
            return True
        source = driver.page_source.lower()
        return "error connecting to match" in source
    except WebDriverException:
        return False


def is_api_abuse_match_error_active(driver: webdriver.Chrome, config: BotConfig) -> bool:
    try:
        if not is_match_error_active(driver, config):
            return False
        source = driver.page_source.lower()
        return "api abuse detected" in source
    except WebDriverException:
        return False


def _is_shutdown_transport_error(error: BaseException) -> bool:
    text = str(error).lower()
    shutdown_markers = (
        "max retries exceeded",
        "failed to establish a new connection",
        "connection aborted",
        "connection reseterror",
        "winerror 10061",
        "winerror 10054",
        "actively refused it",
        "forcibly closed by the remote host",
        "httpconnectionpool(host='localhost'",
        "httpconnection(host='localhost'",
    )
    return any(marker in text for marker in shutdown_markers)


def _is_terminal_network_error(error: BaseException) -> bool:
    text = str(error).lower()
    network_markers = (
        "err_proxy_auth_unsupported",
        "err_tunnel_connection_failed",
    )
    return any(marker in text for marker in network_markers)


def _is_shared_infra_collapse_error(error: BaseException) -> bool:
    text = str(error).lower()
    collapse_markers = (
        "api abuse match-error persisted",
        "match-connection error persisted after refresh recovery attempts",
        "socket/server error persisted after refresh recovery attempts",
        "press-start state repeated without confirmed active chat",
        "press-start state persisted without confirmed active chat",
        "loading state persisted after refresh recovery attempts",
    )
    return any(marker in text for marker in collapse_markers)


def maybe_send_recovery_opener(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    state: dict[str, int],
    *,
    log_when_sent: str,
    log_when_skipped: str,
) -> float | None:
    opener = config.preset_responses[0]
    if _has_any_opener_part_outgoing(driver, config, opener):
        state["next_response_index"] = 1
        state["awaiting_reply"] = 0 if not config.respond_only_on_reply else 1
        _sync_state_inbound_baseline(driver, config, state)
        log_ctx(logger, logging.INFO, ctx, log_when_skipped)
        _log_inbound_snapshot(
            driver,
            config,
            logger,
            ctx,
            label=f"{log_when_skipped} baseline",
            state=state,
        )
        return None

    confirmed_at = _send_configured_opener(
        driver,
        config,
        opener,
        logger=logger,
        ctx=ctx,
        send_label=log_when_sent,
    )
    state["next_response_index"] = 1
    state["awaiting_reply"] = 0 if not config.respond_only_on_reply else 1
    _sync_state_inbound_baseline(driver, config, state)
    log_ctx(logger, logging.INFO, ctx, log_when_sent)
    _log_inbound_snapshot(
        driver,
        config,
        logger,
        ctx,
        label=f"{log_when_sent} baseline",
        state=state,
    )
    return confirmed_at


def run_session(
    user_id: str,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    stop_event: Event,
    *,
    startup_probe_only: bool = False,
    on_ban_detected: Callable[[], None] | None = None,
) -> SessionResult:
    client = AdsPowerClient(config)
    driver: webdriver.Chrome | None = None
    browser_started = False
    attempts = 0
    chatted = False
    opener_sends = 0
    cta_sends = 0
    blank_page_refresh_attempts = 2
    ban_signal_emitted = False

    def _emit_ban_detected() -> None:
        nonlocal ban_signal_emitted
        if ban_signal_emitted:
            return
        ban_signal_emitted = True
        if on_ban_detected is not None:
            try:
                on_ban_detected()
            except Exception:
                pass

    def _raise_startup_ban_detected(message: str) -> None:
        log_ctx(logger, logging.ERROR, ctx, message)
        _emit_ban_detected()
        raise StartupBanDetected(message)

    for attempt in range(1, config.max_session_restarts + 2):
        attempts = attempt
        try:
            if not config.preset_responses:
                raise ValueError("preset_responses is empty")

            browser = client.start_browser(user_id)
            browser_started = True

            options = Options()
            options.add_experimental_option("debuggerAddress", browser.debugger_address)
            service = Service(executable_path=browser.webdriver_path)
            driver = webdriver.Chrome(service=service, options=options)
            _normalize_browser_window(driver)
            _ensure_active_thundr_tab(driver, config.target_url)
            if dismiss_promo_popup(driver, config, logger, ctx, "page-load promo dismiss"):
                log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed on page load")

            wait = WebDriverWait(driver, config.element_wait_seconds)
            startup_hybrid_recovery_attempts = 0
            max_startup_hybrid_recovery_attempts = 1
            startup_malformed_recovery_attempts = 0
            max_startup_malformed_recovery_attempts = 1

            def _click_start_for_startup() -> None:
                if is_press_start_continue_state(driver, config):
                    if click_new_chat(driver, config, logger, ctx, "chat-startup press-start"):
                        log_ctx(logger, logging.INFO, ctx, "Start button clicked")
                        if not _wait_for_post_start_state(driver, config):
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                "Post-start wait did not observe a valid transition yet; continuing with deeper startup checks",
                            )
                    else:
                        log_ctx(
                            logger,
                            logging.WARNING,
                            ctx,
                            "Press-start page was detected during chat startup, but the Start control was not clickable",
                        )
                else:
                    try:
                        start_button = wait.until(EC.element_to_be_clickable(_selector_locator(config.start_button_selector)))
                        _click_element_with_fallbacks(driver, start_button, retries=3)
                        log_ctx(logger, logging.INFO, ctx, "Start button clicked")
                        if not _wait_for_post_start_state(driver, config):
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                "Post-start wait did not observe a valid transition yet; continuing with deeper startup checks",
                            )
                    except TimeoutException:
                        log_ctx(logger, logging.INFO, ctx, "Start button missing; continuing")

            def _recover_auth_chat_hybrid_startup_state(phase: str) -> bool:
                nonlocal startup_hybrid_recovery_attempts
                if not _is_auth_chat_hybrid_state(driver, config):
                    return False
                if startup_hybrid_recovery_attempts >= max_startup_hybrid_recovery_attempts:
                    _log_chat_ready_diagnostics(
                        driver,
                        config,
                        logger,
                        ctx,
                        label=f"{phase} hybrid exhaustion",
                    )
                    raise RuntimeError("Mixed auth/chat interface persisted during chat startup after refresh recovery")

                startup_hybrid_recovery_attempts += 1
                current_url = _normalize_message_text(driver.current_url or "")
                markers = _auth_chat_hybrid_marker_summary(driver, config)
                marker_text = " ".join(f"{name}={visible}" for name, visible in markers.items())
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        f"Mixed auth/chat interface detected during {phase}; "
                        f"url={current_url or '<blank>'} {marker_text}; refreshing chat page"
                    ),
                )
                _ensure_active_thundr_tab(driver, config.target_url)
                driver.refresh()
                if dismiss_promo_popup(driver, config, logger, ctx, f"{phase} hybrid recovery promo dismiss"):
                    log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed during hybrid startup recovery")
                return True

            def _recover_malformed_chat_startup_state(phase: str) -> bool:
                nonlocal startup_malformed_recovery_attempts
                if not _is_malformed_chat_startup_shell(driver, config):
                    return False
                if startup_malformed_recovery_attempts >= max_startup_malformed_recovery_attempts:
                    _log_chat_ready_diagnostics(
                        driver,
                        config,
                        logger,
                        ctx,
                        label=f"{phase} malformed-shell exhaustion",
                    )
                    raise RuntimeError("Malformed chat startup shell persisted after refresh recovery")

                startup_malformed_recovery_attempts += 1
                layout_signals = _chat_shell_layout_signals(driver)
                current_url = _normalize_message_text(driver.current_url or "")
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        f"Malformed chat startup shell detected during {phase}; "
                        f"url={current_url or '<blank>'} "
                        f"scroll_ratio={float(layout_signals['scroll_ratio']):.2f} "
                        f"stacked_landing={bool(layout_signals['stacked_landing'])}; "
                        "refreshing chat page"
                    ),
                )
                _ensure_active_thundr_tab(driver, config.target_url)
                driver.refresh()
                if dismiss_promo_popup(driver, config, logger, ctx, f"{phase} malformed recovery promo dismiss"):
                    log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed during malformed startup recovery")
                return True

            def _wait_for_valid_startup_transition(phase: str) -> None:
                deadline = time.monotonic() + max(float(config.element_wait_seconds), 10.0)
                while time.monotonic() < deadline:
                    if is_banned(driver, config):
                        _raise_startup_ban_detected(
                            f"Ban detected during {phase}; stopping session"
                        )
                    if _recover_auth_chat_hybrid_startup_state(f"{phase} transition"):
                        _click_start_for_startup()
                        deadline = time.monotonic() + max(float(config.element_wait_seconds), 10.0)
                        time.sleep(config.recovery_poll_seconds)
                        continue
                    if _recover_malformed_chat_startup_state(f"{phase} transition"):
                        if not ensure_chat_page_ready(driver, config):
                            raise RuntimeError("Malformed startup shell recovery did not return to a chat-ready page state")
                        _click_start_for_startup()
                        deadline = time.monotonic() + max(float(config.element_wait_seconds), 10.0)
                        time.sleep(config.recovery_poll_seconds)
                        continue
                    if (
                        _is_confirmed_active_chat(driver, config)
                        or is_looking_for_chat_state(driver, config)
                    ):
                        return
                    time.sleep(config.recovery_poll_seconds)

                if is_banned(driver, config):
                    _raise_startup_ban_detected(
                        f"Ban detected during {phase} timeout diagnostics; stopping session"
                    )
                _log_chat_ready_diagnostics(
                    driver,
                    config,
                    logger,
                    ctx,
                    label=f"{phase} transition timeout",
                )
                raise RuntimeError("Startup stabilization timed out without a valid chat transition")

            ready = ensure_chat_page_ready(driver, config)
            if ready and _recover_auth_chat_hybrid_startup_state("chat-startup initial state"):
                ready = ensure_chat_page_ready(driver, config) and not _is_auth_chat_hybrid_state(driver, config)

            if not ready:
                ready = False
                for attempt_index in range(1, blank_page_refresh_attempts + 1):
                    if _recover_auth_chat_hybrid_startup_state(
                        f"chat-startup readiness check {attempt_index}/{blank_page_refresh_attempts}"
                    ):
                        if ensure_chat_page_ready(driver, config) and not _is_auth_chat_hybrid_state(driver, config):
                            ready = True
                            break
                    if is_banned(driver, config):
                        _raise_startup_ban_detected("Ban detected during chat startup; stopping session")
                    _log_chat_ready_diagnostics(
                        driver,
                        config,
                        logger,
                        ctx,
                        label=f"chat-startup pre-refresh {attempt_index}/{blank_page_refresh_attempts}",
                    )
                    if is_probably_blank_chat_page(driver, config):
                        log_ctx(
                            logger,
                            logging.WARNING,
                            ctx,
                            (
                                "Blank chat page detected after initial navigation; "
                                f"refreshing ({attempt_index}/{blank_page_refresh_attempts})"
                            ),
                        )
                    else:
                        log_ctx(
                            logger,
                            logging.WARNING,
                            ctx,
                            (
                                "Chat page not ready after initial navigation; "
                                f"refreshing ({attempt_index}/{blank_page_refresh_attempts})"
                            ),
                        )
                    _ensure_active_thundr_tab(driver, config.target_url)
                    driver.refresh()
                    if dismiss_promo_popup(driver, config, logger, ctx, "chat-page retry promo dismiss"):
                        log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed during chat-page retry")
                    if ensure_chat_page_ready(driver, config):
                        ready = True
                        break
                    if is_banned(driver, config):
                        _raise_startup_ban_detected(
                            "Ban detected during chat startup recovery; stopping session"
                        )
                    _log_chat_ready_diagnostics(
                        driver,
                        config,
                        logger,
                        ctx,
                        label=f"chat-startup post-refresh {attempt_index}/{blank_page_refresh_attempts}",
                    )
                if not ready:
                    _log_chat_ready_diagnostics(
                        driver,
                        config,
                        logger,
                        ctx,
                        label="chat-startup final failure",
                    )
                    if is_banned(driver, config):
                        _raise_startup_ban_detected(
                            "Ban detected during final chat startup diagnostics; stopping session"
                        )
                    if is_api_abuse_match_error_active(driver, config):
                        raise RuntimeError("API abuse match-error persisted during chat startup")
                    raise RuntimeError("Unable to reach chat-ready page state after blank-page recovery attempts")

            _click_start_for_startup()
            if _recover_auth_chat_hybrid_startup_state("chat-startup post-start"):
                if not ensure_chat_page_ready(driver, config):
                    raise RuntimeError("Mixed auth/chat interface recovery did not return to a chat-ready page state")
                _click_start_for_startup()

            check_required_selectors(driver, config)
            if _is_auth_chat_hybrid_state(driver, config):
                _log_chat_ready_diagnostics(
                    driver,
                    config,
                    logger,
                    ctx,
                    label="chat-startup post-health hybrid failure",
                )
                raise RuntimeError("Invalid auth/chat hybrid state survived chat startup selector health check")
            if _recover_malformed_chat_startup_state("chat-startup post-health"):
                if not ensure_chat_page_ready(driver, config):
                    raise RuntimeError("Malformed startup shell recovery did not return to a chat-ready page state")
                _click_start_for_startup()
                check_required_selectors(driver, config)
            _wait_for_valid_startup_transition("chat-startup post-health")
            log_ctx(logger, logging.INFO, ctx, "Selector health check passed")
            if startup_probe_only:
                log_ctx(logger, logging.INFO, ctx, "Startup probe succeeded; chat-ready state reached")
                return SessionResult(
                    status="success",
                    attempts=attempt,
                    chatted=False,
                    opener_sends=0,
                    cta_sends=0,
                )
            initial_incoming_count = incoming_message_count(driver, config)

            state = {
                "next_response_index": 0,
                "last_message_count": initial_incoming_count,
                "awaiting_reply": 0,
                "inbound_count_at_last_outbound": initial_incoming_count,
            }
            consecutive_errors = 0
            last_incoming_ts = time.monotonic()
            last_outbound_confirmed_ts = last_incoming_ts
            last_progress_ts = last_incoming_ts
            cta_sent_ts: float | None = None
            single_message_rotate_deadline_ts: float | None = None
            last_swipe_recovery_ts = 0.0
            loading_started_ts: float | None = None
            loading_timeout_seconds: float | None = None
            loading_refresh_attempts = 0
            looking_started_ts: float | None = None
            looking_timeout_seconds: float | None = None
            socket_error_started_ts: float | None = None
            socket_error_refresh_attempts = 0
            match_error_refresh_attempts = 0
            disconnect_recovery_cooldown_until = 0.0
            disconnected_started_ts: float | None = None
            proactive_follow_up_due_ts: float | None = None
            press_start_consecutive_attempts = 0
            press_start_episode_started_ts: float | None = None
            transition_instability_events = 0
            post_cta_delay_seconds = random.uniform(
                min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
            )

            def mark_progress(*, reset_transition_instability: bool = False) -> None:
                nonlocal last_progress_ts, press_start_consecutive_attempts
                nonlocal press_start_episode_started_ts, transition_instability_events
                last_progress_ts = time.monotonic()
                press_start_consecutive_attempts = 0
                press_start_episode_started_ts = None
                if reset_transition_instability:
                    transition_instability_events = 0

            def note_transition_instability(reason: str) -> None:
                nonlocal transition_instability_events
                transition_instability_events += 1
                log_ctx(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        f"{reason} "
                        f"(transition instability {transition_instability_events}/"
                        f"{config.transition_instability_max_events})"
                    ),
                )
                if transition_instability_events >= config.transition_instability_max_events:
                    raise RuntimeError(
                        "Session exceeded transition instability budget; restarting browser session"
                    )

            def note_press_start_attempt() -> None:
                nonlocal press_start_consecutive_attempts, press_start_episode_started_ts
                now_ts = time.monotonic()
                if press_start_episode_started_ts is None:
                    press_start_episode_started_ts = now_ts
                press_start_consecutive_attempts += 1
                episode_elapsed_seconds = now_ts - press_start_episode_started_ts
                max_press_start_episode_seconds = max(
                    15.0,
                    float(config.press_start_max_consecutive_attempts)
                    * max(
                        2.0,
                        float(getattr(config, "rotate_click_grace_seconds", 3.0)),
                    ),
                )
                if press_start_consecutive_attempts >= config.press_start_max_consecutive_attempts:
                    raise RuntimeError(
                        "Press-start state repeated without confirmed active chat; restarting browser session"
                    )
                if episode_elapsed_seconds >= max_press_start_episode_seconds:
                    raise RuntimeError(
                        "Press-start state persisted without confirmed active chat; restarting browser session"
                    )

            def reset_chat_state_for_recovery() -> None:
                nonlocal state
                nonlocal last_incoming_ts, last_outbound_confirmed_ts, last_progress_ts
                nonlocal cta_sent_ts, single_message_rotate_deadline_ts
                nonlocal loading_started_ts, loading_timeout_seconds
                nonlocal looking_started_ts, looking_timeout_seconds
                nonlocal socket_error_started_ts
                nonlocal disconnect_recovery_cooldown_until, disconnected_started_ts, proactive_follow_up_due_ts
                nonlocal post_cta_delay_seconds, chatted

                base_count = incoming_message_count(driver, config)
                state = {
                    "next_response_index": 0,
                    "last_message_count": base_count,
                    "awaiting_reply": 0,
                    "inbound_count_at_last_outbound": base_count,
                }
                cta_sent_ts = None
                single_message_rotate_deadline_ts = None
                loading_started_ts = None
                loading_timeout_seconds = None
                looking_started_ts = None
                looking_timeout_seconds = None
                socket_error_started_ts = None
                disconnect_recovery_cooldown_until = (
                    time.monotonic() + config.disconnect_recovery_grace_seconds
                )
                disconnected_started_ts = None
                proactive_follow_up_due_ts = None
                post_cta_delay_seconds = random.uniform(
                    min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                    max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                )
                last_incoming_ts = time.monotonic()
                last_outbound_confirmed_ts = last_incoming_ts
                last_progress_ts = last_incoming_ts
                chatted = False

            def rearm_matchmaking_after_refresh(action_label: str) -> None:
                check_required_selectors(driver, config)
                reset_chat_state_for_recovery()

                post_refresh_rearm_deadline = time.monotonic() + max(
                    float(getattr(config, "rotate_click_grace_seconds", 3.0)),
                    3.0,
                )

                while time.monotonic() < post_refresh_rearm_deadline:
                    if dismiss_promo_popup(driver, config, logger, ctx, f"{action_label} promo dismiss"):
                        log_ctx(logger, logging.INFO, ctx, f"Promo popup dismissed during {action_label}")

                    if is_press_start_continue_state(driver, config):
                        note_press_start_attempt()
                        log_ctx(
                            logger,
                            logging.INFO,
                            ctx,
                            f"{action_label}: refresh returned to press-start state; clicking Start before resuming",
                        )
                        if click_new_chat(driver, config, logger, ctx, f"{action_label} press-start"):
                            disconnect_recovery_cooldown_until = (
                                time.monotonic() + config.disconnect_recovery_grace_seconds
                            )
                            _wait_for_post_start_state(driver, config)
                        else:
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                f"{action_label}: press-start state was visible after refresh but Start stayed blocked",
                            )
                        break

                    if (
                        _is_confirmed_active_chat(driver, config)
                        or is_looking_for_chat_state(driver, config)
                        or has_available_new_chat_button(driver, config)
                    ):
                        break

                    time.sleep(config.recovery_poll_seconds)

            def attempt_disconnect_style_recovery(
                *,
                trigger_message: str,
                action_label: str,
                refresh_action_label: str,
                waiting_log_message: str | None = None,
            ) -> bool:
                nonlocal last_outbound_confirmed_ts
                nonlocal opener_sends
                nonlocal single_message_rotate_deadline_ts
                nonlocal proactive_follow_up_due_ts
                nonlocal chatted
                nonlocal disconnected_started_ts
                nonlocal disconnect_recovery_cooldown_until

                log_ctx(logger, logging.INFO, ctx, trigger_message)
                opener = config.preset_responses[0]
                prior_transcript_signature = _chat_transcript_signature(driver, config)
                recovery_ready_timeout = min(
                    max(4.0, float(config.disconnect_ready_timeout_seconds)),
                    8.0,
                )
                if _stabilize_rotate_recovery(
                    driver,
                    config,
                    logger,
                    ctx,
                    action_label=action_label,
                    opener=opener,
                    ready_timeout_seconds=recovery_ready_timeout,
                    local_attempts=config.disconnect_local_recovery_attempts,
                    prior_transcript_signature=prior_transcript_signature,
                    allow_matchmaking_rearm=True,
                ):
                    check_required_selectors(driver, config)
                    reset_chat_state_for_recovery()
                    disconnect_recovery_cooldown_until = time.monotonic()
                    disconnected_started_ts = None
                    opener_confirmed_ts = None
                    if (
                        config.send_first_message
                        and _wait_for_stable_active_chat_window(
                            driver,
                            config,
                            timeout_seconds=2.5,
                        )
                    ):
                        opener_confirmed_ts = _send_opener_with_rotate_guard(
                            driver,
                            config,
                            logger,
                            ctx,
                            action_label=action_label,
                            message=opener,
                        )
                    if opener_confirmed_ts is not None:
                        last_outbound_confirmed_ts = opener_confirmed_ts
                        opener_sends += 1
                        state["next_response_index"] = 1
                        state["awaiting_reply"] = 0 if not config.respond_only_on_reply else 1
                        _sync_state_inbound_baseline(driver, config, state)
                        if config.single_message_mode:
                            single_message_rotate_deadline_ts = (
                                time.monotonic() + config.single_message_rotate_after_seconds
                            )
                        elif not config.respond_only_on_reply:
                            proactive_follow_up_due_ts = _next_reply_due_ts(config)
                        chatted = True
                        mark_progress(reset_transition_instability=True)
                        _log_inbound_snapshot(
                            driver,
                            config,
                            logger,
                            ctx,
                            label=f"Sent opener after {action_label} baseline",
                            state=state,
                        )
                        log_ctx(logger, logging.INFO, ctx, f"Sent opener after {action_label}")
                    elif waiting_log_message is not None and config.send_first_message:
                        log_ctx(logger, logging.INFO, ctx, waiting_log_message)
                    return True

                log_ctx(logger, logging.INFO, ctx, f"{action_label.capitalize()} did not stabilize quickly; refreshing")
                driver.refresh()
                rearm_matchmaking_after_refresh(refresh_action_label)
                disconnected_started_ts = None
                return True

            if (
                config.send_first_message
                and _has_visible_element(driver, config.active_chat_marker_selector)
                and not is_looking_for_chat_state(driver, config)
                and not is_disconnected(driver, config)
            ):
                opener = config.preset_responses[0]
                opener_confirmed_ts = _send_configured_opener(
                    driver,
                    config,
                    opener,
                    logger=logger,
                    ctx=ctx,
                    send_label="initial opener",
                )
                if opener_confirmed_ts is not None:
                    last_outbound_confirmed_ts = opener_confirmed_ts
                opener_sends += 1
                state["next_response_index"] = 1
                state["awaiting_reply"] = 0 if not config.respond_only_on_reply else 1
                _sync_state_inbound_baseline(driver, config, state)
                if config.single_message_mode:
                    single_message_rotate_deadline_ts = time.monotonic() + config.single_message_rotate_after_seconds
                elif not config.respond_only_on_reply:
                    proactive_follow_up_due_ts = _next_reply_due_ts(config)
                chatted = True
                mark_progress(reset_transition_instability=True)
                _log_inbound_snapshot(
                    driver,
                    config,
                    logger,
                    ctx,
                    label="Sent opener message baseline",
                    state=state,
                )
                log_ctx(logger, logging.INFO, ctx, "Sent opener message")

            while not stop_event.is_set():
                try:
                    now = time.monotonic()
                    if (now - last_progress_ts) >= config.no_progress_timeout_seconds:
                        raise RuntimeError(
                            "Session made no meaningful progress for "
                            f"{config.no_progress_timeout_seconds:.1f}s; restarting browser session"
                        )
                    previous_count = state["last_message_count"]
                    state = process_incoming_messages(driver, state, config, logger, ctx)
                    if state["last_message_count"] > previous_count:
                        last_incoming_ts = now
                        if state["awaiting_reply"]:
                            state["awaiting_reply"] = 0
                        mark_progress()

                    disconnected_visible = is_disconnected(driver, config)
                    if disconnected_visible:
                        if disconnected_started_ts is None:
                            disconnected_started_ts = now
                    else:
                        disconnected_started_ts = None

                    if is_promo_popup_active(driver, config):
                        log_ctx(
                            logger,
                            logging.INFO,
                            ctx,
                            "Promo popup detected as blocking overlay; attempting dismissal",
                        )
                        dismissed = dismiss_promo_popup(
                            driver,
                            config,
                            logger,
                            ctx,
                            "blocking promo overlay dismiss",
                        )
                        if dismissed:
                            log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed from blocking overlay state")
                            disconnect_recovery_cooldown_until = (
                                time.monotonic() + config.disconnect_recovery_grace_seconds
                            )
                            continue
                        note_transition_instability(
                            "Promo popup remained blocking after dismissal attempts; refreshing page"
                        )
                        driver.refresh()
                        rearm_matchmaking_after_refresh("blocking promo-overlay recovery")
                        continue

                    if is_press_start_continue_state(driver, config):
                        note_press_start_attempt()
                        log_ctx(
                            logger,
                            logging.INFO,
                            ctx,
                            (
                                "Detected 'Press start to continue' state; clicking Start to resume matchmaking "
                                f"({press_start_consecutive_attempts}/"
                                f"{config.press_start_max_consecutive_attempts})"
                            ),
                        )
                        if dismiss_promo_popup(driver, config, logger, ctx, "press-start promo dismiss"):
                            log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed before press-start continue")
                        if not click_new_chat(driver, config, logger, ctx, "press-start continue"):
                            note_transition_instability(
                                "Press-start-continue control stayed blocked; refreshing page for recovery"
                            )
                            driver.refresh()
                            if dismiss_promo_popup(driver, config, logger, ctx, "press-start recovery promo dismiss"):
                                log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed after press-start recovery")
                            check_required_selectors(driver, config)
                            base_count = incoming_message_count(driver, config)
                            state = {
                                "next_response_index": 0,
                                "last_message_count": base_count,
                                "awaiting_reply": 0,
                                "inbound_count_at_last_outbound": base_count,
                            }
                            cta_sent_ts = None
                            single_message_rotate_deadline_ts = None
                            loading_started_ts = None
                            loading_timeout_seconds = None
                            disconnect_recovery_cooldown_until = (
                                time.monotonic() + config.disconnect_recovery_grace_seconds
                            )
                            post_cta_delay_seconds = random.uniform(
                                min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                            )
                            last_incoming_ts = time.monotonic()
                            last_outbound_confirmed_ts = last_incoming_ts
                            continue
                        disconnect_recovery_cooldown_until = (
                            time.monotonic() + config.disconnect_recovery_grace_seconds
                        )
                        disconnected_started_ts = None
                        continue

                    if is_looking_for_chat_state(driver, config):
                        if looking_started_ts is None:
                            looking_started_ts = now
                            looking_timeout_seconds = random.uniform(
                                min(
                                    config.looking_stuck_timeout_min_seconds,
                                    config.looking_stuck_timeout_max_seconds,
                                ),
                                max(
                                    config.looking_stuck_timeout_min_seconds,
                                    config.looking_stuck_timeout_max_seconds,
                                ),
                            )
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Detected looking-for-chat state; "
                                    f"waiting up to {looking_timeout_seconds:.1f}s for active chat"
                                ),
                            )
                        elif looking_timeout_seconds is not None and (
                            now - looking_started_ts
                        ) >= looking_timeout_seconds:
                            note_transition_instability(
                                "Looking-for-chat state persisted without active chat; refreshing page"
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("looking-state recovery")
                            continue
                    else:
                        looking_started_ts = None
                        looking_timeout_seconds = None

                    if is_loading_chat_transition(driver, config) and not has_available_new_chat_button(driver, config):
                        if loading_started_ts is None:
                            loading_started_ts = now
                            loading_timeout_seconds = random.uniform(
                                min(
                                    config.loading_stuck_timeout_min_seconds,
                                    config.loading_stuck_timeout_max_seconds,
                                ),
                                max(
                                    config.loading_stuck_timeout_min_seconds,
                                    config.loading_stuck_timeout_max_seconds,
                                ),
                            )
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Detected chat transition stuck on Loading...; "
                                    f"waiting up to {loading_timeout_seconds:.1f}s before refresh"
                                ),
                            )
                        elif loading_timeout_seconds is not None and (
                            now - loading_started_ts
                        ) >= loading_timeout_seconds:
                            loading_refresh_attempts += 1
                            note_transition_instability("Loading... persisted without New/Start returning")
                            if loading_refresh_attempts > config.loading_recovery_max_attempts:
                                raise RuntimeError(
                                    "Loading state persisted after refresh recovery attempts; restarting browser session"
                                )
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Loading... persisted without New/Start returning; "
                                    f"refreshing page ({loading_refresh_attempts}/{config.loading_recovery_max_attempts})"
                                ),
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("loading-state recovery")
                            continue
                    else:
                        loading_refresh_attempts = 0
                        loading_started_ts = None
                        loading_timeout_seconds = None

                    if is_banned(driver, config):
                        log_ctx(logger, logging.ERROR, ctx, "Ban detected for profile; stopping session")
                        _emit_ban_detected()
                        return SessionResult(
                            status="banned",
                            attempts=attempt,
                            fatal_error="BANNED",
                            chatted=chatted,
                            opener_sends=opener_sends,
                            cta_sends=cta_sends,
                        )

                    if is_socket_error_active(driver, config):
                        if socket_error_started_ts is None:
                            socket_error_started_ts = now
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Socket/server error detected; waiting up to "
                                    f"{config.socket_error_self_recover_seconds:.1f}s for in-place recovery"
                                ),
                            )

                        if _is_socket_recovery_actionable(driver, config):
                            socket_error_started_ts = None
                            socket_error_refresh_attempts = 0
                            attempt_disconnect_style_recovery(
                                trigger_message=(
                                    "Socket/server error entered actionable recovery state; "
                                    "attempting immediate Start/New Chat recovery"
                                ),
                                action_label="socket-error recovery",
                                refresh_action_label="socket-error recovery",
                                waiting_log_message=(
                                    "Socket/server recovery re-armed matchmaking; "
                                    "waiting for active match before sending opener"
                                ),
                            )
                            continue

                        if (now - socket_error_started_ts) < config.socket_error_self_recover_seconds:
                            time.sleep(config.recovery_poll_seconds)
                            continue

                        socket_error_refresh_attempts += 1
                        socket_error_started_ts = None
                        note_transition_instability("Socket error detected")
                        if socket_error_refresh_attempts <= config.socket_error_refresh_attempts:
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Socket/server error stayed stuck; refreshing page "
                                    f"({socket_error_refresh_attempts}/{config.socket_error_refresh_attempts})"
                                ),
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("socket-error recovery")
                            continue

                        raise RuntimeError(
                            "Socket/server error persisted after refresh recovery attempts; restarting browser session"
                        )
                    else:
                        socket_error_started_ts = None
                        socket_error_refresh_attempts = 0

                    if is_match_error_active(driver, config):
                        match_error_refresh_attempts += 1
                        note_transition_instability("Match-connection error detected")
                        if match_error_refresh_attempts <= config.match_error_refresh_attempts:
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                (
                                    "Match-connection error detected; refreshing page "
                                    f"({match_error_refresh_attempts}/{config.match_error_refresh_attempts})"
                                ),
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("match-error recovery")
                            continue

                        raise RuntimeError(
                            "API abuse match-error persisted after refresh recovery attempts"
                            if is_api_abuse_match_error_active(driver, config)
                            else "Match-connection error persisted after refresh recovery attempts; restarting browser session"
                        )
                    else:
                        match_error_refresh_attempts = 0

                    if is_swipe_gate_active(driver, config):
                        if now - last_swipe_recovery_ts >= config.swipe_gate_recovery_cooldown_seconds:
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                "Swipe-gate screen detected; attempting refresh recovery",
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("swipe-gate recovery")
                            last_swipe_recovery_ts = now
                            continue

                    disconnect_recovery_ready = bool(
                        disconnected_visible
                        and (
                            has_available_new_chat_button(driver, config)
                            or is_press_start_continue_state(driver, config)
                            or (
                                disconnected_started_ts is not None
                                and (now - disconnected_started_ts)
                                >= max(0.75, config.recovery_poll_seconds * 2.0)
                            )
                        )
                    )

                    if disconnect_recovery_ready:
                        attempt_disconnect_style_recovery(
                            trigger_message="Visible disconnected state detected; attempting immediate Start/New Chat recovery",
                            action_label="disconnect recovery",
                            refresh_action_label="disconnect refresh recovery",
                            waiting_log_message=(
                                "Disconnect recovery re-armed matchmaking; "
                                "waiting for active match before sending opener"
                            ),
                        )
                        continue

                    recovery_active = (
                        looking_started_ts is not None
                        or loading_started_ts is not None
                        or socket_error_refresh_attempts > 0
                        or time.monotonic() < disconnect_recovery_cooldown_until
                    )
                    if (
                        config.send_first_message
                        and not chatted
                        and not recovery_active
                        and _has_visible_element(driver, config.active_chat_marker_selector)
                        and not is_looking_for_chat_state(driver, config)
                        and not is_disconnected(driver, config)
                    ):
                        if not _wait_for_stable_active_chat_window(
                            driver,
                            config,
                            timeout_seconds=2.0,
                        ):
                            time.sleep(config.recovery_poll_seconds)
                            continue
                        opener_confirmed_ts = maybe_send_recovery_opener(
                            driver,
                            config,
                            logger,
                            ctx,
                            state,
                            log_when_sent="Sent opener after active match became available",
                            log_when_skipped="Skipped opener after active match became available; opener already present in current chat",
                        )
                        if opener_confirmed_ts is not None:
                            last_outbound_confirmed_ts = opener_confirmed_ts
                            opener_sends += 1
                        if config.single_message_mode:
                            single_message_rotate_deadline_ts = (
                                time.monotonic() + config.single_message_rotate_after_seconds
                            )
                        elif not config.respond_only_on_reply:
                            proactive_follow_up_due_ts = _next_reply_due_ts(config)
                        chatted = True
                        mark_progress(reset_transition_instability=True)
                    if (
                        not config.single_message_mode
                        and not config.respond_only_on_reply
                        and not recovery_active
                        and cta_sent_ts is None
                        and state["next_response_index"] > 0
                        and proactive_follow_up_due_ts is None
                    ):
                        proactive_follow_up_due_ts = _next_reply_due_ts(config)

                    if (
                        config.single_message_mode
                        and single_message_rotate_deadline_ts is not None
                        and not recovery_active
                        and state["last_message_count"] > state["inbound_count_at_last_outbound"]
                    ):
                        incoming_text = _normalize_message_text(latest_incoming_message(driver, config))
                        opener_texts = {_normalize_message_text(part) for part in _opener_parts(config, config.preset_responses[0])}
                        if incoming_text and incoming_text in opener_texts:
                            log_ctx(
                                logger,
                                logging.INFO,
                                ctx,
                                "Single-message mode detected mirrored opener from stranger; rotating chat immediately",
                            )
                            single_message_rotate_deadline_ts = time.monotonic()

                    if (
                        config.single_message_mode
                        and single_message_rotate_deadline_ts is not None
                        and not recovery_active
                        and time.monotonic() >= single_message_rotate_deadline_ts
                    ):
                        log_ctx(logger, logging.INFO, ctx, "Single-message rotate deadline elapsed; rotating chat")
                        if _attempt_rotate_transition(
                            driver,
                            config,
                            logger,
                            ctx,
                            action_label="single-message rotate",
                        ):
                            opener = config.preset_responses[0]
                            if not _wait_for_post_rotate_chat_ready(
                                driver,
                                config,
                                logger,
                                ctx,
                                action_label="single-message rotate",
                                opener=opener,
                            ):
                                log_ctx(
                                    logger,
                                    logging.WARNING,
                                    ctx,
                                    "Single-message rotate did not reach a confirmed fresh chat; refreshing page for recovery",
                                )
                                note_transition_instability(
                                    "Single-message rotate did not reach a confirmed fresh chat"
                                )
                                driver.refresh()
                                if dismiss_promo_popup(
                                    driver,
                                    config,
                                    logger,
                                    ctx,
                                    "single-message post-rotate readiness recovery promo dismiss",
                                ):
                                    log_ctx(
                                        logger,
                                        logging.INFO,
                                        ctx,
                                        "Promo popup dismissed after single-message post-rotate readiness recovery",
                                    )
                                check_required_selectors(driver, config)
                                base_count = incoming_message_count(driver, config)
                                state = {
                                    "next_response_index": 0,
                                    "last_message_count": base_count,
                                    "awaiting_reply": 0,
                                    "inbound_count_at_last_outbound": base_count,
                                }
                                cta_sent_ts = None
                                single_message_rotate_deadline_ts = None
                                loading_started_ts = None
                                loading_timeout_seconds = None
                                disconnect_recovery_cooldown_until = (
                                    time.monotonic() + config.disconnect_recovery_grace_seconds
                                )
                                post_cta_delay_seconds = random.uniform(
                                    min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                    max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                )
                                last_incoming_ts = time.monotonic()
                                last_outbound_confirmed_ts = last_incoming_ts
                                if config.send_first_message:
                                    opener_confirmed_ts = maybe_send_recovery_opener(
                                        driver,
                                        config,
                                        logger,
                                        ctx,
                                        state,
                                        log_when_sent="Sent opener after single-message stale-chat recovery",
                                        log_when_skipped="Skipped opener after single-message stale-chat recovery; opener already present in current chat",
                                    )
                                    if opener_confirmed_ts is not None:
                                        last_outbound_confirmed_ts = opener_confirmed_ts
                                        opener_sends += 1
                                    single_message_rotate_deadline_ts = (
                                        time.monotonic() + config.single_message_rotate_after_seconds
                                    )
                                    chatted = True
                                    mark_progress(reset_transition_instability=True)
                                continue
                            base_count = incoming_message_count(driver, config)
                            state = {
                                "next_response_index": 0,
                                "last_message_count": base_count,
                                "awaiting_reply": 0,
                                "inbound_count_at_last_outbound": base_count,
                            }
                            cta_sent_ts = None
                            single_message_rotate_deadline_ts = None
                            loading_started_ts = None
                            loading_timeout_seconds = None
                            disconnect_recovery_cooldown_until = (
                                time.monotonic() + config.disconnect_recovery_grace_seconds
                            )
                            post_cta_delay_seconds = random.uniform(
                                min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                            )
                            last_incoming_ts = time.monotonic()
                            last_outbound_confirmed_ts = last_incoming_ts
                            if config.send_first_message:
                                opener_confirmed_ts = _send_opener_with_rotate_guard(
                                    driver,
                                    config,
                                    logger,
                                    ctx,
                                    action_label="single-message rotate",
                                    message=opener,
                                )
                                if opener_confirmed_ts is None:
                                    log_ctx(
                                        logger,
                                        logging.WARNING,
                                        ctx,
                                        "Single-message rotate opener step failed; refreshing page for recovery",
                                    )
                                    driver.refresh()
                                    if dismiss_promo_popup(
                                        driver,
                                        config,
                                        logger,
                                        ctx,
                                        "single-message rotate opener recovery promo dismiss",
                                    ):
                                        log_ctx(
                                            logger,
                                            logging.INFO,
                                            ctx,
                                            "Promo popup dismissed after single-message opener recovery",
                                        )
                                    check_required_selectors(driver, config)
                                    base_count = incoming_message_count(driver, config)
                                    state = {
                                        "next_response_index": 0,
                                        "last_message_count": base_count,
                                        "awaiting_reply": 0,
                                        "inbound_count_at_last_outbound": base_count,
                                    }
                                    cta_sent_ts = None
                                    single_message_rotate_deadline_ts = None
                                    loading_started_ts = None
                                    loading_timeout_seconds = None
                                    disconnect_recovery_cooldown_until = (
                                        time.monotonic() + config.disconnect_recovery_grace_seconds
                                    )
                                    post_cta_delay_seconds = random.uniform(
                                        min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                        max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                                    )
                                    last_incoming_ts = time.monotonic()
                                    last_outbound_confirmed_ts = last_incoming_ts
                                    continue
                                last_outbound_confirmed_ts = opener_confirmed_ts
                                opener_sends += 1
                                state["next_response_index"] = 1
                                state["awaiting_reply"] = 1
                                _sync_state_inbound_baseline(driver, config, state)
                                single_message_rotate_deadline_ts = (
                                    time.monotonic() + config.single_message_rotate_after_seconds
                                )
                                chatted = True
                                mark_progress(reset_transition_instability=True)
                                log_ctx(logger, logging.INFO, ctx, "Sent opener in single-message new chat")
                            continue
                        note_transition_instability(
                            "Single-message rotate control stayed blocked; refreshing page for recovery"
                        )
                        driver.refresh()
                        if dismiss_promo_popup(driver, config, logger, ctx, "single-message rotate recovery promo dismiss"):
                            log_ctx(logger, logging.INFO, ctx, "Promo popup dismissed after single-message rotate recovery")
                        check_required_selectors(driver, config)
                        base_count = incoming_message_count(driver, config)
                        state = {
                            "next_response_index": 0,
                            "last_message_count": base_count,
                            "awaiting_reply": 0,
                            "inbound_count_at_last_outbound": base_count,
                        }
                        cta_sent_ts = None
                        single_message_rotate_deadline_ts = None
                        loading_started_ts = None
                        loading_timeout_seconds = None
                        disconnect_recovery_cooldown_until = (
                            time.monotonic() + config.disconnect_recovery_grace_seconds
                        )
                        post_cta_delay_seconds = random.uniform(
                            min(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                            max(config.post_cta_delay_min_seconds, config.post_cta_delay_max_seconds),
                        )
                        last_incoming_ts = time.monotonic()
                        last_outbound_confirmed_ts = last_incoming_ts
                        if config.send_first_message:
                            opener_confirmed_ts = maybe_send_recovery_opener(
                                driver,
                                config,
                                logger,
                                ctx,
                                state,
                                log_when_sent="Sent opener after blocked single-message rotate recovery",
                                log_when_skipped="Skipped opener after blocked single-message rotate recovery; opener already present in current chat",
                            )
                            if opener_confirmed_ts is not None:
                                last_outbound_confirmed_ts = opener_confirmed_ts
                                opener_sends += 1
                            if config.single_message_mode:
                                single_message_rotate_deadline_ts = (
                                    time.monotonic() + config.single_message_rotate_after_seconds
                                )
                            chatted = True
                            mark_progress(reset_transition_instability=True)
                        continue

                    if (
                        not config.single_message_mode
                        and not recovery_active
                        and
                        cta_sent_ts is not None
                        and config.post_cta_skip
                        and (time.monotonic() - cta_sent_ts) >= post_cta_delay_seconds
                    ):
                        log_ctx(
                            logger,
                            logging.INFO,
                            ctx,
                            (
                                "Post-CTA delay elapsed; rotating chat "
                                f"(delay={post_cta_delay_seconds:.1f}s)"
                            ),
                        )
                        opener = config.preset_responses[0]
                        prior_transcript_signature = _chat_transcript_signature(driver, config)
                        if _stabilize_rotate_recovery(
                            driver,
                            config,
                            logger,
                            ctx,
                            action_label="post-CTA rotate",
                            opener=opener,
                            ready_timeout_seconds=config.post_cta_ready_timeout_seconds,
                            local_attempts=config.post_cta_local_recovery_attempts,
                            prior_transcript_signature=prior_transcript_signature,
                            allow_matchmaking_rearm=True,
                        ):
                            check_required_selectors(driver, config)
                            reset_chat_state_for_recovery()
                            disconnect_recovery_cooldown_until = time.monotonic()
                            if config.send_first_message and _is_confirmed_active_chat(driver, config):
                                opener_confirmed_ts = _send_opener_with_rotate_guard(
                                    driver,
                                    config,
                                    logger,
                                    ctx,
                                    action_label="post-CTA rotate",
                                    message=opener,
                                )
                                if opener_confirmed_ts is None:
                                    log_ctx(
                                        logger,
                                        logging.WARNING,
                                        ctx,
                                        "Post-CTA rotate opener step failed; handing back to recovery loop without refresh",
                                    )
                                    continue
                                last_outbound_confirmed_ts = opener_confirmed_ts
                                opener_sends += 1
                                state["next_response_index"] = 1
                                state["awaiting_reply"] = 0 if not config.respond_only_on_reply else 1
                                _sync_state_inbound_baseline(driver, config, state)
                                if not config.respond_only_on_reply:
                                    proactive_follow_up_due_ts = _next_reply_due_ts(config)
                                chatted = True
                                mark_progress(reset_transition_instability=True)
                                _log_inbound_snapshot(
                                    driver,
                                    config,
                                    logger,
                                    ctx,
                                    label="Sent opener in post-CTA new chat baseline",
                                    state=state,
                                )
                                log_ctx(logger, logging.INFO, ctx, "Sent opener in post-CTA new chat")
                            elif config.send_first_message:
                                log_ctx(
                                    logger,
                                    logging.INFO,
                                    ctx,
                                    "Post-CTA rotate re-armed matchmaking; waiting for active match before sending opener",
                                )
                        else:
                            log_ctx(
                                logger,
                                logging.WARNING,
                                ctx,
                                "Post-CTA rotate did not reach a confirmed fresh chat after in-place retries; refreshing page for recovery",
                            )
                            note_transition_instability(
                                "Post-CTA rotate did not reach a confirmed fresh chat"
                            )
                            driver.refresh()
                            rearm_matchmaking_after_refresh("post-CTA stale-chat recovery")
                            continue

                    if (
                        time.monotonic() >= disconnect_recovery_cooldown_until
                        and disconnected_visible
                    ):
                        attempt_disconnect_style_recovery(
                            trigger_message="Disconnect detected; attempting immediate Start/New Chat recovery",
                            action_label="disconnect recovery",
                            refresh_action_label="disconnect refresh recovery",
                            waiting_log_message=(
                                "Disconnect recovery re-armed matchmaking; "
                                "waiting for active match before sending opener"
                            ),
                        )
                        continue

                    if (
                        not config.single_message_mode
                        and config.respond_only_on_reply
                        and not recovery_active
                        and
                        cta_sent_ts is None
                        and state["awaiting_reply"]
                        and time.monotonic()
                        - max(last_incoming_ts, last_outbound_confirmed_ts)
                        >= config.no_reply_timeout_seconds
                    ):
                        cta_message = config.preset_responses[-1]
                        silence_anchor_ts = max(last_incoming_ts, last_outbound_confirmed_ts)
                        _log_inbound_snapshot(
                            driver,
                            config,
                            logger,
                            ctx,
                            label=(
                                "No-reply CTA trigger"
                                f" | silence_for={time.monotonic() - silence_anchor_ts:.1f}s"
                            ),
                            state=state,
                        )
                        cta_confirmed_ts = send_chat_message(
                            driver,
                            config,
                            cta_message,
                            logger=logger,
                            ctx=ctx,
                            send_label="no-reply CTA",
                        )
                        last_outbound_confirmed_ts = cta_confirmed_ts
                        chatted = True
                        cta_sends += 1
                        cta_sent_ts = time.monotonic()
                        state["awaiting_reply"] = 0
                        mark_progress(reset_transition_instability=True)
                        log_ctx(
                            logger,
                            logging.INFO,
                            ctx,
                            (
                                f"No incoming message for {config.no_reply_timeout_seconds}s; "
                                f"sent CTA and waiting {post_cta_delay_seconds:.1f}s before rotating chat"
                            ),
                        )

                    if (
                        not config.single_message_mode
                        and not recovery_active
                        and
                        cta_sent_ts is None
                        and not state["awaiting_reply"]
                        and (
                            (
                                config.respond_only_on_reply
                                and state["last_message_count"] > state["inbound_count_at_last_outbound"]
                            )
                            or (
                                not config.respond_only_on_reply
                                and proactive_follow_up_due_ts is not None
                                and time.monotonic() >= proactive_follow_up_due_ts
                            )
                        )
                    ):
                        next_index = state["next_response_index"]
                        if next_index < len(config.preset_responses):
                            response = config.preset_responses[next_index]
                            response_confirmed_ts = send_chat_message(
                                driver,
                                config,
                                response,
                                logger=logger,
                                ctx=ctx,
                                send_label=f"scripted response index={next_index}",
                            )
                            last_outbound_confirmed_ts = response_confirmed_ts
                            chatted = True
                            is_cta = next_index == (len(config.preset_responses) - 1)
                            state["next_response_index"] = next_index + 1
                            state["awaiting_reply"] = 0 if (is_cta or not config.respond_only_on_reply) else 1
                            _sync_state_inbound_baseline(driver, config, state)
                            proactive_follow_up_due_ts = (
                                None
                                if (is_cta or config.respond_only_on_reply)
                                else _next_reply_due_ts(config)
                            )
                            mark_progress(reset_transition_instability=True)
                            log_ctx(
                                logger,
                                logging.INFO,
                                ctx,
                                f"Sent scripted response index={next_index}",
                            )
                            if is_cta:
                                cta_sends += 1
                                cta_sent_ts = time.monotonic()
                                log_ctx(
                                    logger,
                                    logging.INFO,
                                    ctx,
                                    (
                                        "CTA sent in reply flow; waiting "
                                        f"{post_cta_delay_seconds:.1f}s before rotating chat"
                                    ),
                                )

                    consecutive_errors = 0
                    if _use_fast_loop_poll(driver, config):
                        time.sleep(
                            random.uniform(
                                min(config.active_loop_sleep_min_seconds, config.active_loop_sleep_max_seconds),
                                max(config.active_loop_sleep_min_seconds, config.active_loop_sleep_max_seconds),
                            )
                        )
                    else:
                        time.sleep(
                            random.uniform(
                                config.loop_sleep_min_seconds,
                                config.loop_sleep_max_seconds,
                            )
                        )

                except (NoSuchElementException, TimeoutException, WebDriverException) as error:
                    consecutive_errors += 1
                    error_text = str(error).strip() or error.__class__.__name__
                    log_ctx(
                        logger,
                        logging.WARNING,
                        ctx,
                        (
                            "Loop warning "
                            f"({consecutive_errors}/{config.max_consecutive_loop_errors}): {error_text}"
                        ),
                    )
                    if consecutive_errors >= config.max_consecutive_loop_errors:
                        raise RuntimeError("Too many consecutive loop errors") from error
                    time.sleep(min(2 * consecutive_errors, 8))

            log_ctx(logger, logging.INFO, ctx, "Stop signal received; session ending")
            return SessionResult(
                status="stopped",
                attempts=attempt,
                chatted=chatted,
                opener_sends=opener_sends,
                cta_sends=cta_sends,
            )

        except StartupBanDetected:
            return SessionResult(
                status="banned",
                attempts=attempt,
                fatal_error="BANNED",
                chatted=chatted,
                opener_sends=opener_sends,
                cta_sends=cta_sends,
            )

        except Exception as error:  # noqa: BLE001
            terminal_network_error = _is_terminal_network_error(error)
            shared_infra_collapse = _is_shared_infra_collapse_error(error)
            shutdown_transport_error = stop_event.is_set() and _is_shutdown_transport_error(error)
            log_ctx(
                logger,
                logging.WARNING if shutdown_transport_error else logging.ERROR,
                ctx,
                (
                    f"Session attempt {attempt} interrupted during shutdown: {error}"
                    if shutdown_transport_error
                    else f"Session attempt {attempt} hit terminal network/proxy error: {error}"
                    if terminal_network_error
                    else f"Session attempt {attempt} entered parked shared-infra collapse state: {error}"
                    if shared_infra_collapse
                    else f"Session attempt {attempt} failed: {error}"
                ),
            )

            if shared_infra_collapse and not stop_event.is_set():
                return SessionResult(
                    status="parked",
                    attempts=attempt,
                    fatal_error=f"SHARED_INFRA_COLLAPSE: {error}",
                    chatted=chatted,
                    opener_sends=opener_sends,
                    cta_sends=cta_sends,
                )

            if terminal_network_error and not stop_event.is_set():
                return SessionResult(
                    status="max_retries",
                    attempts=attempt,
                    fatal_error=str(error),
                    chatted=chatted,
                    opener_sends=opener_sends,
                    cta_sends=cta_sends,
                )

            if attempt > config.max_session_restarts:
                return SessionResult(
                    status="max_retries",
                    attempts=attempt,
                    fatal_error=str(error),
                    chatted=chatted,
                    opener_sends=opener_sends,
                    cta_sends=cta_sends,
                )

            if stop_event.is_set():
                return SessionResult(
                    status="stopped",
                    attempts=attempt,
                    fatal_error=None if shutdown_transport_error else str(error),
                    chatted=chatted,
                    opener_sends=opener_sends,
                    cta_sends=cta_sends,
                )

            time.sleep(config.restart_backoff_seconds * attempt)

        finally:
            if driver is not None:
                if stop_event.is_set():
                    # On cooperative shutdown, stop the AdsPower browser directly and avoid
                    # blocking on a stale Selenium connection before the worker can report results.
                    driver = None
                else:
                    try:
                        driver.quit()
                    except Exception as quit_error:  # noqa: BLE001
                        log_ctx(logger, logging.WARNING, ctx, f"driver.quit failed: {quit_error}")
                driver = None

            if browser_started:
                try:
                    client.stop_browser(user_id)
                except Exception as stop_error:  # noqa: BLE001
                    log_ctx(logger, logging.WARNING, ctx, f"stop_browser failed: {stop_error}")
                browser_started = False

    return SessionResult(
        status="failed",
        attempts=attempts,
        fatal_error="Unexpected exit",
        chatted=chatted,
        opener_sends=opener_sends,
        cta_sends=cta_sends,
    )
