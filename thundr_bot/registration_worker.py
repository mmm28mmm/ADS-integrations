from __future__ import annotations

import logging
import random
import string
import time
from datetime import date, timedelta
from threading import Event

from selenium import webdriver
from selenium.common.exceptions import ElementNotInteractableException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from thundr_bot.adspower_client import AdsPowerClient
from thundr_bot.config import BotConfig, RuntimeContext


class RetryableRegistrationError(RuntimeError):
    """Raised when registration should restart the browser/profile and retry."""


def _log(logger: logging.Logger, level: int, ctx: RuntimeContext, message: str) -> None:
    logger.log(
        level,
        message,
        extra={"run_id": ctx.run_id, "session_id": ctx.session_id, "user_id": ctx.user_id},
    )


def _selector_locator(selector: str) -> tuple[str, str]:
    selector = selector.strip()
    if selector.lower().startswith("xpath="):
        return (By.XPATH, selector.split("=", 1)[1].strip())
    return (By.CSS_SELECTOR, selector)


def _is_css_selector(selector: str) -> bool:
    return not selector.strip().lower().startswith("xpath=")


def _wait_clickable(driver: webdriver.Chrome, selector: str, timeout: int):
    wait = WebDriverWait(driver, timeout)
    return wait.until(EC.element_to_be_clickable(_selector_locator(selector)))


def _wait_present(driver: webdriver.Chrome, selector: str, timeout: int):
    wait = WebDriverWait(driver, timeout)
    return wait.until(EC.presence_of_element_located(_selector_locator(selector)))


def _wait_visible(driver: webdriver.Chrome, selector: str, timeout: int):
    wait = WebDriverWait(driver, timeout)
    return wait.until(EC.visibility_of_element_located(_selector_locator(selector)))


def _action_pause(config: BotConfig) -> None:
    low = min(config.registration_action_delay_min_seconds, config.registration_action_delay_max_seconds)
    high = max(config.registration_action_delay_min_seconds, config.registration_action_delay_max_seconds)
    if high <= 0:
        return
    time.sleep(random.uniform(max(0.0, low), high))


def _click(driver: webdriver.Chrome, selector: str, timeout: int, config: BotConfig | None = None) -> None:
    if config is not None:
        _action_pause(config)
    element = _wait_clickable(driver, selector, timeout)
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)
    if config is not None:
        _action_pause(config)


def _set_input(
    driver: webdriver.Chrome,
    selector: str,
    value: str,
    timeout: int,
    config: BotConfig | None = None,
) -> None:
    if config is not None:
        _action_pause(config)

    last_error: Exception | None = None
    deadline = time.monotonic() + max(2.0, float(timeout))

    while time.monotonic() < deadline:
        if _js_set_input(driver, selector, value):
            if config is not None:
                _action_pause(config)
            return
        try:
            field = _wait_visible(driver, selector, max(1, min(timeout, 4)))
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                    field,
                )
            except WebDriverException:
                pass

            if config is not None:
                _action_pause(config)

            try:
                field.click()
            except WebDriverException:
                try:
                    ActionChains(driver).move_to_element(field).pause(0.05).click().perform()
                except WebDriverException:
                    try:
                        driver.execute_script("arguments[0].focus();", field)
                    except WebDriverException:
                        pass

            if config is not None:
                _action_pause(config)

            try:
                field.clear()
            except WebDriverException:
                pass

            try:
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(Keys.DELETE)
                field.send_keys(value)
            except WebDriverException as error:
                last_error = error

            current_value = ""
            try:
                current_value = (field.get_attribute("value") or "").strip()
            except WebDriverException:
                current_value = ""

            if current_value == value:
                if config is not None:
                    _action_pause(config)
                return

            try:
                driver.execute_script(
                    """
                    const input = arguments[0];
                    const value = arguments[1];
                    input.focus();
                    input.value = '';
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    field,
                    value,
                )
            except WebDriverException as error:
                last_error = error

            try:
                current_value = (field.get_attribute("value") or "").strip()
            except WebDriverException:
                current_value = ""

            if current_value == value:
                if config is not None:
                    _action_pause(config)
                return

            last_error = RuntimeError(f"Field value mismatch after input set for selector={selector}")
        except (TimeoutException, WebDriverException) as error:
            last_error = error

        time.sleep(0.35)

    if last_error is not None:
        raise last_error
    raise TimeoutException(f"Timed out setting input for selector={selector}")


def _blur_input(driver: webdriver.Chrome, selector: str) -> None:
    try:
        field = driver.find_element(*_selector_locator(selector))
        field.send_keys(Keys.TAB)
    except WebDriverException:
        return


def _exists(driver: webdriver.Chrome, selector: str) -> bool:
    return bool(driver.find_elements(*_selector_locator(selector)))


def _is_visible(driver: webdriver.Chrome, selector: str) -> bool:
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


def _count_matches(driver: webdriver.Chrome, selector: str) -> int:
    try:
        return len(driver.find_elements(*_selector_locator(selector)))
    except WebDriverException:
        return 0


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


def _js_query_count(driver: webdriver.Chrome, selector: str) -> int:
    if not _is_css_selector(selector):
        return 0
    try:
        result = driver.execute_script(
            "return document.querySelectorAll(arguments[0]).length;",
            selector,
        )
        return int(result or 0)
    except WebDriverException:
        return 0


def _js_set_input(driver: webdriver.Chrome, selector: str, value: str) -> bool:
    if not _is_css_selector(selector):
        return False
    try:
        result = driver.execute_script(
            """
            const input = document.querySelector(arguments[0]);
            if (!input) return false;
            input.scrollIntoView({block:'center', inline:'nearest'});
            input.focus();
            input.click();
            input.value = '';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.value = arguments[1];
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return (input.value || '') === arguments[1];
            """,
            selector,
            value,
        )
        return bool(result)
    except WebDriverException:
        return False


def _js_click_selector(driver: webdriver.Chrome, selector: str) -> bool:
    if not _is_css_selector(selector):
        return False
    try:
        result = driver.execute_script(
            """
            const element = document.querySelector(arguments[0]);
            if (!element) return false;
            element.scrollIntoView({block:'center', inline:'nearest'});
            if (typeof element.focus === 'function') element.focus();
            element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            element.click();
            return true;
            """,
            selector,
        )
        return bool(result)
    except WebDriverException:
        return False


def _js_click_xpath(driver: webdriver.Chrome, selector: str) -> bool:
    if not selector.strip().lower().startswith("xpath="):
        return False
    xpath = selector.split("=", 1)[1].strip()
    try:
        result = driver.execute_script(
            """
            const xpath = arguments[0];
            const match = document.evaluate(
                xpath,
                document,
                null,
                XPathResult.FIRST_ORDERED_NODE_TYPE,
                null
            ).singleNodeValue;
            if (!match) return false;
            match.scrollIntoView({block:'center', inline:'nearest'});
            if (typeof match.focus === 'function') match.focus();
            match.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            match.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            match.click();
            return true;
            """,
            xpath,
        )
        return bool(result)
    except WebDriverException:
        return False


def _js_click_acknowledgement_accept(driver: webdriver.Chrome) -> bool:
    try:
        result = driver.execute_script(
            """
            const isVisible = (element) => {
              if (!element) return false;
              const style = window.getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                rect.width > 0 &&
                rect.height > 0;
            };
            const normalizedText = (element) => {
              const parts = [
                element.innerText || '',
                element.textContent || '',
                element.getAttribute('aria-label') || '',
                element.getAttribute('title') || '',
                element.getAttribute('value') || '',
              ];
              return parts.join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
            };
            const candidates = Array.from(
              document.querySelectorAll(
                "button,[role='button'],input[type='button'],input[type='submit'],a"
              )
            );
            for (const candidate of candidates) {
              const text = normalizedText(candidate);
              if (!text || (!text.includes('i agree') && text !== 'agree')) continue;
              if (!isVisible(candidate) || candidate.disabled) continue;
              candidate.scrollIntoView({block:'center', inline:'nearest'});
              if (typeof candidate.focus === 'function') candidate.focus();
              candidate.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
              candidate.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
              candidate.click();
              return true;
            }
            return false;
            """
        )
        return bool(result)
    except WebDriverException:
        return False


def _body_text(driver: webdriver.Chrome) -> str:
    try:
        result = driver.execute_script(
            "return (document.body && (document.body.innerText || document.body.textContent)) || '';"
        )
        return str(result or "")
    except WebDriverException:
        return ""


def _wait_for_acknowledgement_progress(
    driver: webdriver.Chrome,
    config: BotConfig,
    *,
    timeout_seconds: float,
) -> str | None:
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while time.monotonic() < deadline:
        progressed_stage = _post_create_progress_stage(driver, config)
        if progressed_stage and progressed_stage != "acknowledgement":
            return progressed_stage
        if _is_post_registration_success_state(driver, config):
            return "completed"
        time.sleep(0.2)
    return None


def _submit_create_account_form_via_tab_fallback(
    driver: webdriver.Chrome,
    username: str,
    password: str,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
) -> bool:
    def _active_summary() -> dict[str, str]:
        try:
            data = driver.execute_script(
                """
                const el = document.activeElement;
                if (!el) return {};
                return {
                    tag: (el.tagName || '').toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    placeholder: el.getAttribute('placeholder') || '',
                    aria_label: el.getAttribute('aria-label') || '',
                    text: (el.innerText || el.textContent || '').trim(),
                };
                """
            )
            return data or {}
        except WebDriverException:
            return {}

    def _send_tab() -> None:
        if body is not None:
            body.send_keys(Keys.TAB)
        else:
            ActionChains(driver).send_keys(Keys.TAB).perform()
        time.sleep(0.35)

    def _focus_expected(expected_terms: tuple[str, ...], max_tabs: int, label: str) -> bool:
        for _ in range(max_tabs):
            active = _active_summary()
            combined = " ".join(
                (
                    active.get("placeholder", ""),
                    active.get("aria_label", ""),
                    active.get("text", ""),
                    active.get("type", ""),
                )
            ).lower()
            if any(term.lower() in combined for term in expected_terms):
                _log(
                    logger,
                    logging.INFO,
                    ctx,
                    f"Registration flow: keyboard fallback focused {label}",
                )
                return True
            _send_tab()
        return False

    def _type_into_active(value: str) -> None:
        try:
            result = driver.execute_script(
                """
                const el = document.activeElement;
                if (!el || !('value' in el)) return false;
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.value = arguments[0];
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
                """,
                value,
            )
            if result:
                time.sleep(0.35)
                return
        except WebDriverException:
            pass

        try:
            active = driver.switch_to.active_element
            active.send_keys(Keys.CONTROL, "a")
            active.send_keys(Keys.DELETE)
            for char in value:
                active.send_keys(char)
                time.sleep(0.05)
        except WebDriverException:
            ActionChains(driver).send_keys(value).perform()
        time.sleep(0.35)

    try:
        body = driver.find_element(By.TAG_NAME, "body")
    except WebDriverException:
        body = None

    try:
        if body is not None:
            try:
                body.click()
            except WebDriverException:
                pass

        _log(
            logger,
            logging.WARNING,
            ctx,
            "Registration flow: using keyboard fallback for create-account form",
        )

        if not _focus_expected(("choose a unique username",), 2, "username field"):
            _log(logger, logging.WARNING, ctx, "Registration flow: keyboard fallback could not focus username field")
            return False
        _type_into_active(username)
        if not _focus_expected(("choose a password",), 2, "password field"):
            _log(logger, logging.WARNING, ctx, "Registration flow: keyboard fallback could not focus password field")
            return False
        _type_into_active(password)
        if not _focus_expected(("confirm your password", "confirm password"), 3, "confirm password field"):
            _log(logger, logging.WARNING, ctx, "Registration flow: keyboard fallback could not focus confirm password field")
            return False
        _type_into_active(password)
        if not _focus_expected(("create account",), 3, "create account button"):
            _log(logger, logging.WARNING, ctx, "Registration flow: keyboard fallback could not focus create account button")
            return False

        try:
            active = driver.switch_to.active_element
            active.send_keys(Keys.ENTER)
        except WebDriverException:
            if body is not None:
                body.send_keys(Keys.ENTER)
            else:
                ActionChains(driver).send_keys(Keys.ENTER).perform()
        time.sleep(0.5)

        return True
    except WebDriverException:
        return False


def _url_contains(driver: webdriver.Chrome, needle: str) -> bool:
    if not needle:
        return False
    return needle.lower() in _current_url_lower(driver)


def _gen_username(config: BotConfig) -> str:
    prefixes = [p for p in config.registration_username_prefixes if p.strip()]
    prefix = random.choice(prefixes) if prefixes else config.registration_username_prefix
    suffix_min = min(
        config.registration_username_suffix_min_len,
        config.registration_username_suffix_max_len,
    )
    suffix_max = max(
        config.registration_username_suffix_min_len,
        config.registration_username_suffix_max_len,
    )
    suffix_len = random.randint(max(1, suffix_min), max(1, suffix_max))
    charset = config.registration_username_suffix_charset or "0123456789Xx_"

    # Ensure at least one numeric char to look less uniform and avoid empty patterns.
    while True:
        suffix = "".join(random.choice(charset) for _ in range(suffix_len))
        if any(ch.isdigit() for ch in suffix):
            break
    return f"{prefix}{suffix}"


def _gen_password(config: BotConfig) -> str:
    if not config.registration_password_randomize:
        return config.registration_password

    length_min = min(config.registration_password_min_len, config.registration_password_max_len)
    length_max = max(config.registration_password_min_len, config.registration_password_max_len)
    length = random.randint(max(6, length_min), max(6, length_max))
    charset = config.registration_password_charset or (
        string.ascii_letters + string.digits
    )
    return "".join(random.choice(charset) for _ in range(length))


def _choose_birthdate(config: BotConfig) -> tuple[int, int, int]:
    if not config.registration_birth_randomize:
        return (
            config.registration_birth_month,
            config.registration_birth_day,
            config.registration_birth_year,
        )

    start_year = min(config.registration_birth_year_min, config.registration_birth_year_max)
    end_year = max(config.registration_birth_year_min, config.registration_birth_year_max)
    start = date(start_year, 1, 1)
    end = date(end_year, 12, 31)
    delta_days = (end - start).days
    picked = start + timedelta(days=random.randint(0, delta_days))
    return (picked.month, picked.day, picked.year)


def _select_birthday(driver: webdriver.Chrome, config: BotConfig) -> None:
    def _resolve_selects() -> tuple[object, object, object]:
        per_field_timeout = max(2, min(config.element_wait_seconds, 6))
        try:
            month_el = _wait_present(driver, config.reg_bday_month_selector, per_field_timeout)
            day_el = _wait_present(driver, config.reg_bday_day_selector, per_field_timeout)
            year_el = _wait_present(driver, config.reg_bday_year_selector, per_field_timeout)
            return month_el, day_el, year_el
        except TimeoutException:
            selects = driver.find_elements(By.TAG_NAME, "select")
            if len(selects) >= 3:
                return selects[0], selects[1], selects[2]
            raise

    def _set_select_value(element, value: str) -> None:
        try:
            Select(element).select_by_value(value)
            if (element.get_attribute("value") or "") == value:
                return
        except WebDriverException:
            pass

        try:
            result = driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];
                if (!el) return false;
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return (el.value || '') === value;
                """,
                element,
                value,
            )
            if result:
                return
        except WebDriverException:
            pass

        element.click()
        element.send_keys(value)

    month_val, day_val, year_val = _choose_birthdate(config)
    month, day, year = _resolve_selects()
    _action_pause(config)
    _set_select_value(month, str(month_val))
    _action_pause(config)
    _set_select_value(day, str(day_val))
    _action_pause(config)
    _set_select_value(year, str(year_val))
    _action_pause(config)


def _select_dropdown_value(
    driver: webdriver.Chrome,
    selector: str,
    value: str,
    timeout: int,
    config: BotConfig | None = None,
) -> None:
    element = _wait_present(driver, selector, timeout)
    if config is not None:
        _action_pause(config)
    try:
        Select(element).select_by_value(value)
        if (element.get_attribute("value") or "") != value:
            raise WebDriverException("select value did not stick")
    except WebDriverException:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            if (!el) return false;
            el.scrollIntoView({block:'center', inline:'nearest'});
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
            """,
            element,
            value,
        )
    if config is not None:
        _action_pause(config)


def _scroll_and_click(
    driver: webdriver.Chrome,
    selector: str,
    attempts: int = 8,
    config: BotConfig | None = None,
) -> bool:
    locator = _selector_locator(selector)
    for _ in range(attempts):
        matches = driver.find_elements(*locator)
        if matches:
            target = matches[0]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
                if config is not None:
                    _action_pause(config)
                target.click()
            except WebDriverException:
                try:
                    driver.execute_script("arguments[0].click();", target)
                except WebDriverException:
                    pass
            if config is not None:
                _action_pause(config)
            return True
        try:
            driver.execute_script("window.scrollBy(0, 500);")
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
        except WebDriverException:
            pass
        time.sleep(0.3)
    return False


def _scroll_into_view_and_click(driver: webdriver.Chrome, selector: str, config: BotConfig | None = None) -> bool:
    locator = _selector_locator(selector)
    try:
        matches = driver.find_elements(*locator)
    except WebDriverException:
        return False
    for target in matches:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                target,
            )
        except WebDriverException:
            pass
        if config is not None:
            _action_pause(config)
        try:
            target.click()
            if config is not None:
                _action_pause(config)
            return True
        except WebDriverException:
            try:
                driver.execute_script("arguments[0].click();", target)
                if config is not None:
                    _action_pause(config)
                return True
            except WebDriverException:
                continue
    return False


def _click_if_present(driver: webdriver.Chrome, selector: str, timeout: int, config: BotConfig) -> bool:
    try:
        _click(driver, selector, timeout, config=config)
        return True
    except TimeoutException:
        return False


def _click_resilient(driver: webdriver.Chrome, selector: str, timeout: int, config: BotConfig) -> bool:
    locator = _selector_locator(selector)
    deadline = time.monotonic() + max(2.0, float(timeout))

    while time.monotonic() < deadline:
        try:
            elements = driver.find_elements(*locator)
        except WebDriverException:
            elements = []

        candidates = []
        for element in elements:
            try:
                if element.is_displayed():
                    candidates.append(element)
            except WebDriverException:
                continue
        if not candidates and elements:
            candidates = elements

        for element in candidates:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                    element,
                )
            except WebDriverException:
                pass

            try:
                driver.execute_script("arguments[0].focus();", element)
            except WebDriverException:
                pass

            _action_pause(config)

            try:
                element.click()
                _action_pause(config)
                return True
            except (ElementNotInteractableException, WebDriverException):
                pass

            try:
                ActionChains(driver).move_to_element(element).pause(0.05).click().perform()
                _action_pause(config)
                return True
            except WebDriverException:
                pass

            try:
                element.send_keys(Keys.ENTER)
                _action_pause(config)
                return True
            except (ElementNotInteractableException, WebDriverException):
                pass

            try:
                driver.execute_script(
                    """
                    arguments[0].dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    arguments[0].dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    arguments[0].click();
                    """,
                    element,
                )
                _action_pause(config)
                return True
            except WebDriverException:
                continue

        time.sleep(0.35)

    return False


def _is_registration_wizard_visible(driver: webdriver.Chrome, config: BotConfig) -> bool:
    selectors = (
        config.reg_signin_page_marker_selector,
        config.reg_signin_anon_selector,
        config.reg_anon_auth_page_marker_selector,
        config.reg_create_page_marker_selector,
        config.reg_username_selector,
        config.reg_password_selector,
        config.reg_confirm_password_selector,
        config.reg_submit_selector,
        config.reg_ack_page_marker_selector,
        config.reg_bday_page_marker_selector,
        config.reg_gender_page_marker_selector,
        config.reg_meet_page_marker_selector,
    )
    if any(_exists(driver, selector) for selector in selectors):
        return True

    body_text = _body_text(driver).lower()
    text_markers = (
        "you must be at least 18 years old to use thundr",
        "what is your birthday?",
        "what is your sex?",
        "looking to meet?",
        "i agree",
    )
    if any(marker in body_text for marker in text_markers):
        return True

    url = _current_url_lower(driver)
    if "thundr.com/register" in url and not _is_strong_post_registration_state(driver, config):
        return True

    return False


def _is_chat_ready_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    selectors = (
        config.chat_input_selector,
        config.message_container_selector,
    )
    return any(_exists(driver, selector) for selector in selectors)


def _current_url_lower(driver: webdriver.Chrome) -> str:
    try:
        return (driver.current_url or "").strip().lower()
    except WebDriverException:
        return ""


def _is_video_success_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    url = _current_url_lower(driver)
    if "thundr.com/video" in url:
        return True
    return (
        _exists(driver, config.reg_video_no_camera_selector)
        or _exists(driver, config.reg_video_local_selector)
        or _exists(driver, config.reg_video_remote_spinner_selector)
    )


def _is_text_state(driver: webdriver.Chrome) -> bool:
    url = _current_url_lower(driver)
    return "thundr.com/chat?mode=text" in url


def _is_home_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    url = _current_url_lower(driver)
    if not url:
        return False
    if (
        "thundr.com/video" in url
        or "thundr.com/chat?mode=text" in url
        or "thundr.com/register" in url
        or "thundr.com/signin" in url
        or "thundr.com/signup" in url
        or "thundr.com/settings" in url
    ):
        return False
    # Home is where geo button is expected and no active register flow marker should remain.
    return _exists(driver, config.reg_geo_button_selector)


def _is_post_registration_success_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return (
        _is_video_success_state(driver, config)
        or _is_home_state(driver, config)
        or _is_text_state(driver)
        or _is_chat_ready_state(driver, config)
    )


def _is_strong_post_registration_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return _is_video_success_state(driver, config)


def _post_create_progress_stage(driver: webdriver.Chrome, config: BotConfig) -> str | None:
    body_text = _body_text(driver).lower()
    if _exists(driver, config.reg_ack_page_marker_selector):
        return "acknowledgement"
    if (
        "you must be at least 18 years old to use thundr" in body_text
        or ("i agree" in body_text and "terms of service" in body_text)
    ):
        return "acknowledgement"
    if _exists(driver, config.reg_bday_page_marker_selector):
        return "birthday"
    if "what is your birthday?" in body_text:
        return "birthday"
    if _exists(driver, config.reg_gender_page_marker_selector):
        return "gender"
    if "what is your sex?" in body_text:
        return "gender"
    if _exists(driver, config.reg_meet_page_marker_selector):
        return "meet"
    if "looking to meet?" in body_text:
        return "meet"
    if _is_strong_post_registration_state(driver, config):
        return "completed"
    return None


def _is_signin_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return _exists(driver, config.reg_signin_page_marker_selector) or _exists(driver, config.reg_signin_anon_selector)


def _is_signup_state(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return _exists(driver, config.reg_create_account_selector) or _exists(driver, config.reg_create_page_marker_selector)


def _selectors_for_stage(stage: str, config: BotConfig) -> tuple[str, ...]:
    stage_selectors: dict[str, tuple[str, ...]] = {
        "post_start": (
            config.reg_signin_page_marker_selector,
            config.reg_signin_anon_selector,
            config.reg_anon_auth_page_marker_selector,
            config.reg_create_page_marker_selector,
        ),
        "await_create_account": (
            config.reg_create_account_selector,
            config.reg_anon_auth_page_marker_selector,
            config.reg_create_page_marker_selector,
        ),
        "create_account_form": (
            config.reg_create_page_marker_selector,
            config.reg_username_selector,
            config.reg_password_selector,
            config.reg_confirm_password_selector,
        ),
        "acknowledgement": (
            config.reg_ack_page_marker_selector,
            config.reg_ack_agree_selector,
        ),
        "birthday": (
            config.reg_bday_page_marker_selector,
            config.reg_bday_month_selector,
            config.reg_bday_day_selector,
            config.reg_bday_year_selector,
        ),
        "gender": (
            config.reg_gender_page_marker_selector,
            config.reg_gender_select_selector,
            config.reg_gender_female_selector,
            config.reg_gender_next_selector,
        ),
        "meet": (
            config.reg_meet_page_marker_selector,
            config.reg_meet_select_selector,
            config.reg_meet_everyone_selector,
            config.reg_meet_start_selector,
        ),
    }
    return stage_selectors.get(stage, ())


def _settle_post_start_stage(
    driver: webdriver.Chrome,
    current_stage: str,
    config: BotConfig,
    attempts: int = 3,
    pause_seconds: float = 0.75,
) -> bool:
    for _ in range(max(1, attempts)):
        if (
            _exists(driver, config.reg_create_page_marker_selector)
            or _exists(driver, config.reg_signin_page_marker_selector)
            or _exists(driver, config.reg_signin_anon_selector)
            or _exists(driver, config.reg_anon_auth_page_marker_selector)
            or _is_registration_wizard_visible(driver, config)
            or _is_strong_post_registration_state(driver, config)
            or _has_expected_stage_signal(driver, current_stage, config)
        ):
            return True
        time.sleep(max(0.0, pause_seconds))
    return False


def _has_expected_stage_signal(driver: webdriver.Chrome, stage: str, config: BotConfig) -> bool:
    return any(_exists(driver, selector) for selector in _selectors_for_stage(stage, config))


def _is_auth_flow_visible(driver: webdriver.Chrome, config: BotConfig) -> bool:
    selectors = (
        config.reg_signin_page_marker_selector,
        config.reg_signin_anon_selector,
        config.reg_anon_auth_page_marker_selector,
        config.reg_create_account_selector,
        config.reg_create_page_marker_selector,
    )
    return any(_exists(driver, selector) for selector in selectors)


def _is_auth_route(url: str) -> bool:
    return any(
        route in url
        for route in (
            "thundr.com/signin",
            "thundr.com/signup",
            "thundr.com/register",
        )
    )


def _mixed_auth_chat_marker_summary(driver: webdriver.Chrome, config: BotConfig) -> dict[str, bool]:
    return {
        "start": _is_visible(driver, config.reg_start_selector),
        "input": _is_visible(driver, config.chat_input_selector),
        "send": _is_visible(driver, config.send_button_selector),
        "messages": _is_visible(driver, config.message_container_selector),
    }


def _is_mixed_auth_chat_interface(driver: webdriver.Chrome, config: BotConfig) -> bool:
    url = _current_url_lower(driver)
    if not _is_auth_route(url):
        return False

    markers = _mixed_auth_chat_marker_summary(driver, config)
    visible_marker_count = sum(1 for visible in markers.values() if visible)
    return visible_marker_count >= 2


def _recover_mixed_auth_chat_interface(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    *,
    phase: str,
) -> None:
    url = _current_url_lower(driver)
    markers = _mixed_auth_chat_marker_summary(driver, config)
    marker_text = " ".join(f"{name}={visible}" for name, visible in markers.items())
    _log(
        logger,
        logging.WARNING,
        ctx,
        (
            f"Registration flow: mixed auth/chat interface detected during {phase}; "
            f"url={url or '<blank>'} {marker_text}; refreshing Thundr page"
        ),
    )
    driver.refresh()
    _action_pause(config)


def _wait_for_expected_stage_signal(
    driver: webdriver.Chrome,
    stage: str,
    config: BotConfig,
    timeout_seconds: float = 3.0,
) -> bool:
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while time.monotonic() < deadline:
        if _has_expected_stage_signal(driver, stage, config):
            return True
        if _is_registration_wizard_visible(driver, config):
            return True
        if _is_strong_post_registration_state(driver, config):
            return True
        time.sleep(0.25)
    return _has_expected_stage_signal(driver, stage, config)


def _wait_for_post_registration_state(driver: webdriver.Chrome, config: BotConfig, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if _is_post_registration_success_state(driver, config):
            return True
        if not _is_registration_wizard_visible(driver, config):
            return True
        time.sleep(0.5)
    return _is_post_registration_success_state(driver, config) or (not _is_registration_wizard_visible(driver, config))


def _wait_for_strong_post_registration_state(
    driver: webdriver.Chrome,
    config: BotConfig,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if _is_strong_post_registration_state(driver, config):
            return True
        time.sleep(0.5)
    return _is_strong_post_registration_state(driver, config)


def _configure_geo_preferences_best_effort(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
) -> bool:
    try:
        driver.get(config.registration_geo_url)
        _action_pause(config)
        _wait_present(driver, config.reg_geo_page_marker_selector, config.element_wait_seconds)

        if _exists(driver, config.reg_geo_deselect_all_selector):
            _click(driver, config.reg_geo_deselect_all_selector, config.element_wait_seconds, config=config)

        for country_selector in (
            config.reg_geo_country_au_selector,
            config.reg_geo_country_ca_selector,
            config.reg_geo_country_us_selector,
            config.reg_geo_country_uk_selector,
        ):
            if not _scroll_into_view_and_click(driver, country_selector, config=config):
                _log(logger, logging.WARNING, ctx, f"Could not select country selector: {country_selector}")

        selected_text = _wait_present(
            driver,
            config.reg_geo_selected_count_selector,
            config.element_wait_seconds,
        ).text
        if "4" not in selected_text:
            _log(logger, logging.WARNING, ctx, f"Unexpected selected country count text: {selected_text}")

        if _exists(driver, config.reg_geo_done_selector):
            _click(driver, config.reg_geo_done_selector, config.element_wait_seconds, config=config)

        _log(logger, logging.INFO, ctx, "Geo selection completed")
        return True
    except Exception as error:  # noqa: BLE001
        _log(logger, logging.WARNING, ctx, f"Geo selection failed; continuing without it: {error}")
        return False


def _navigate_to_signup_fallback(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    reason: str,
) -> bool:
    try:
        driver.get(config.registration_signup_url)
        _action_pause(config)
        if _wait_for_expected_stage_signal(driver, "await_create_account", config, timeout_seconds=4.0):
            _log(
                logger,
                logging.WARNING,
                ctx,
                f"{reason}; navigated directly to signup as fallback",
            )
            return True
    except WebDriverException:
        pass
    return False


def _describe_handle_registration_context(
    driver: webdriver.Chrome,
    config: BotConfig,
    handle: str,
) -> tuple[int, str, str]:
    try:
        driver.switch_to.window(handle)
    except WebDriverException:
        return (-1, "", "unavailable")

    url = _current_url_lower(driver)
    try:
        title = (driver.title or "").strip()
    except WebDriverException:
        title = ""

    is_thundr_handle = "thundr.com" in url
    is_adspower_handle = "start.adspower.net" in url

    username_count = _count_matches(driver, config.reg_username_selector)
    password_count = _count_matches(driver, config.reg_password_selector)
    confirm_count = _count_matches(driver, config.reg_confirm_password_selector)
    submit_count = _count_matches(driver, config.reg_submit_selector)
    signin_count = _count_matches(driver, config.reg_signin_anon_selector)
    create_account_count = _count_matches(driver, config.reg_create_account_selector)
    ack_count = _count_matches(driver, config.reg_ack_page_marker_selector)
    js_username_count = _js_query_count(driver, config.reg_username_selector)
    js_password_count = _js_query_count(driver, config.reg_password_selector)
    js_confirm_count = _js_query_count(driver, config.reg_confirm_password_selector)
    js_submit_count = _js_query_count(driver, config.reg_submit_selector)

    score = (
        (100 if is_thundr_handle else 0)
        - (100 if is_adspower_handle else 0)
        +
        (max(username_count, js_username_count) * 5)
        + (max(password_count, js_password_count) * 5)
        + (max(confirm_count, js_confirm_count) * 5)
        + (max(submit_count, js_submit_count) * 4)
        + (ack_count * 3)
        + (create_account_count * 2)
        + signin_count
    )
    details = (
        f"url={url or '<blank>'} title={title or '<blank>'} "
        f"is_thundr={is_thundr_handle} is_adspower={is_adspower_handle} "
        f"username={username_count} password={password_count} confirm={confirm_count} "
        f"submit={submit_count} js_username={js_username_count} js_password={js_password_count} "
        f"js_confirm={js_confirm_count} js_submit={js_submit_count} "
        f"create_button={create_account_count} signin={signin_count} ack={ack_count}"
    )
    return (score, handle, details)


def _switch_to_best_registration_context(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    reason: str,
) -> bool:
    try:
        current_handle = driver.current_window_handle
        handles = list(driver.window_handles)
    except WebDriverException:
        return False

    best_score = -1
    best_handle = current_handle
    best_details = ""
    inspected: list[str] = []
    try:
        current_url = _current_url_lower(driver)
    except WebDriverException:
        current_url = ""

    for handle in handles:
        score, inspected_handle, details = _describe_handle_registration_context(driver, config, handle)
        if score >= 0:
            inspected.append(f"{inspected_handle}: {details}")
        if score > best_score:
            best_score = score
            best_handle = inspected_handle
            best_details = details
        elif score == best_score and "thundr.com" in current_url and inspected_handle == current_handle:
            best_handle = inspected_handle
            best_details = details

    _log(
        logger,
        logging.INFO,
        ctx,
        f"Registration flow: context probe ({reason}) handles={len(handles)} best_score={best_score} best={best_details}",
    )
    for details in inspected:
        _log(logger, logging.INFO, ctx, f"Registration flow: handle probe {details}")

    try:
        driver.switch_to.window(best_handle)
    except WebDriverException:
        return False

    return best_score > 0


def _is_create_account_form_visible(driver: webdriver.Chrome, config: BotConfig) -> bool:
    return (
        _exists(driver, config.reg_create_page_marker_selector)
        or _exists(driver, config.reg_confirm_password_selector)
        or (
            _exists(driver, config.reg_username_selector)
            and _exists(driver, config.reg_password_selector)
            and _exists(driver, config.reg_submit_selector)
        )
        or _js_query_count(driver, config.reg_username_selector) > 0
        or _js_query_count(driver, config.reg_confirm_password_selector) > 0
        or (
            _js_query_count(driver, config.reg_username_selector) > 0
            and _js_query_count(driver, config.reg_password_selector) > 0
            and _js_query_count(driver, config.reg_submit_selector) > 0
        )
    )


def _wait_for_create_account_form_transition(
    driver: webdriver.Chrome,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    timeout_seconds: float | None = None,
) -> bool:
    deadline = time.monotonic() + max(2.0, timeout_seconds or float(config.element_wait_seconds))
    mixed_interface_refresh_attempted = False
    _switch_to_best_registration_context(
        driver,
        config,
        logger,
        ctx,
        reason="post-create-account-click",
    )
    while time.monotonic() < deadline:
        if _is_mixed_auth_chat_interface(driver, config):
            if mixed_interface_refresh_attempted:
                time.sleep(0.35)
            else:
                mixed_interface_refresh_attempted = True
                _recover_mixed_auth_chat_interface(
                    driver,
                    config,
                    logger,
                    ctx,
                    phase="create-account transition",
                )
                _switch_to_best_registration_context(
                    driver,
                    config,
                    logger,
                    ctx,
                    reason="post-mixed-auth-chat-refresh",
                )
            continue

        if _is_create_account_form_visible(driver, config):
            return True

        if _exists(driver, config.reg_ack_page_marker_selector):
            return True

        time.sleep(0.35)

    try:
        current_handle = driver.current_window_handle
    except WebDriverException:
        current_handle = "<unavailable>"
    _log(
        logger,
        logging.WARNING,
        ctx,
        (
            "Registration flow: create-account transition timed out; "
            f"handle={current_handle} "
            f"url={_current_url_lower(driver)} "
            f"username={_exists(driver, config.reg_username_selector)} "
            f"password={_exists(driver, config.reg_password_selector)} "
            f"confirm={_exists(driver, config.reg_confirm_password_selector)} "
            f"submit={_exists(driver, config.reg_submit_selector)} "
            f"js_username={_js_query_count(driver, config.reg_username_selector)} "
            f"js_password={_js_query_count(driver, config.reg_password_selector)} "
            f"js_confirm={_js_query_count(driver, config.reg_confirm_password_selector)} "
            f"js_submit={_js_query_count(driver, config.reg_submit_selector)}"
        ),
    )
    return False


def _ensure_active_thundr_tab(driver: webdriver.Chrome, url: str) -> None:
    target_host = "thundr.com"

    # Prefer an already-open Thundr tab if present.
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            current = (driver.current_url or "").lower()
            if target_host in current:
                driver.get(url)
                return
        except WebDriverException:
            continue

    # Otherwise open a fresh tab and navigate explicitly.
    try:
        driver.switch_to.new_window("tab")
    except WebDriverException:
        # Fallback for environments that do not support new_window reliably.
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])
    driver.get(url)


def _run_registration_once(
    user_id: str,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    stop_event: Event,
) -> bool:
    client = AdsPowerClient(config)
    driver: webdriver.Chrome | None = None
    browser_started = False
    force_strong_completion = user_id in config.unregistered_user_ids
    current_stage = "post_start"
    mixed_interface_refreshes = 0
    max_mixed_interface_refreshes = 2

    try:
        browser = client.start_browser(user_id)
        browser_started = True

        options = Options()
        options.add_experimental_option("debuggerAddress", browser.debugger_address)
        service = Service(executable_path=browser.webdriver_path)
        driver = webdriver.Chrome(service=service, options=options)
        _normalize_browser_window(driver)

        _ensure_active_thundr_tab(driver, config.registration_home_url)
        _log(logger, logging.INFO, ctx, "Registration flow: thundr tab focused")

        # Attempt to enter the sign-in flow when starting from homepage.
        start_clicked = _click_if_present(driver, config.reg_start_selector, timeout=4, config=config)
        clicked_sign_in_anon_directly = False
        if start_clicked:
            _log(logger, logging.INFO, ctx, "Registration flow: start button clicked")
        else:
            _log(
                logger,
                logging.WARNING,
                ctx,
                "Registration flow: start button missing; trying sign-in fallback",
            )
            if _click_if_present(driver, config.reg_signin_anon_selector, timeout=4, config=config):
                clicked_sign_in_anon_directly = True
                current_stage = "await_create_account"
                _log(
                    logger,
                    logging.INFO,
                    ctx,
                    "Registration flow: sign-in fallback clicked",
                )
            else:
                _log(
                    logger,
                    logging.WARNING,
                    ctx,
                    "Registration flow: sign-in fallback missing; resetting to homepage and retrying",
                )
                driver.get(config.registration_home_url)
                _action_pause(config)
                start_clicked = _click_if_present(driver, config.reg_start_selector, timeout=6, config=config)
                if start_clicked:
                    _log(logger, logging.INFO, ctx, "Registration flow: start button clicked after homepage reset")
                elif _click_if_present(driver, config.reg_signin_anon_selector, timeout=4, config=config):
                    clicked_sign_in_anon_directly = True
                    current_stage = "await_create_account"
                    _log(
                        logger,
                        logging.INFO,
                        ctx,
                        "Registration flow: sign-in fallback clicked after homepage reset",
                    )

        # Resolve page state after Start/home navigation.
        # Older behavior was more stable here: wait for the next useful registration
        # state instead of aggressively resetting homepage/start when the transition
        # is briefly ambiguous.
        post_start_deadline = time.monotonic() + max(15.0, float(config.element_wait_seconds) * 2.0)
        while True:
            if _is_mixed_auth_chat_interface(driver, config):
                if mixed_interface_refreshes >= max_mixed_interface_refreshes:
                    raise RetryableRegistrationError(
                        "Mixed auth/chat interface persisted on auth route during registration startup"
                    )
                mixed_interface_refreshes += 1
                _recover_mixed_auth_chat_interface(
                    driver,
                    config,
                    logger,
                    ctx,
                    phase=f"post-start state resolution ({mixed_interface_refreshes}/{max_mixed_interface_refreshes})",
                )
                continue

            if _exists(driver, config.reg_create_page_marker_selector):
                current_stage = "create_account_form"
                _log(logger, logging.INFO, ctx, "Registration flow: create account page already active")
                break
            if clicked_sign_in_anon_directly:
                _log(logger, logging.INFO, ctx, "Registration flow: waiting for anon-auth page after sign-in click")
                _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account from anon-auth page")
                if not _click_resilient(driver, config.reg_create_account_selector, config.element_wait_seconds, config):
                    raise RuntimeError(
                        "Create Account button not clickable after sign-in fallback click; "
                        f"current_url={driver.current_url}"
                    )
                if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        "Create-account form was not detectable after sign-in fallback click; continuing with keyboard fallback",
                    )
                current_stage = "create_account_form"
                break
            if _is_signin_state(driver, config) or _exists(driver, config.reg_signin_page_marker_selector):
                _log(logger, logging.INFO, ctx, "Registration flow: sign-in page detected")
                if not _click_resilient(driver, config.reg_signin_anon_selector, config.element_wait_seconds, config):
                    if not _navigate_to_signup_fallback(
                        driver,
                        config,
                        logger,
                        ctx,
                        "Sign In Anonymously button not clickable on sign-in page",
                    ):
                        raise RuntimeError(
                            "Sign In Anonymously button not clickable on sign-in page; "
                            f"current_url={driver.current_url}"
                        )
                current_stage = "await_create_account"
                _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account from anon-auth page")
                if not _click_resilient(driver, config.reg_create_account_selector, config.element_wait_seconds, config):
                    raise RuntimeError(
                        "Create Account button not clickable after entering anon auth page; "
                        f"current_url={driver.current_url}"
                    )
                if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        "Create-account form was not detectable after entering anon auth page; continuing with keyboard fallback",
                    )
                current_stage = "create_account_form"
                break
            if _exists(driver, config.reg_anon_auth_page_marker_selector) or (
                _is_signup_state(driver, config) and not _exists(driver, config.reg_create_page_marker_selector)
            ):
                _log(logger, logging.INFO, ctx, "Registration flow: anon-auth marker detected")
                _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account from anon-auth marker page")
                if not _click_resilient(driver, config.reg_create_account_selector, config.element_wait_seconds, config):
                    raise RuntimeError(
                        "Create Account button not clickable on anon-auth marker page; "
                        f"current_url={driver.current_url}"
                    )
                if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        "Create-account form was not detectable after anon-auth marker page click; continuing with keyboard fallback",
                    )
                current_stage = "create_account_form"
                break
            if _is_auth_flow_visible(driver, config):
                _log(logger, logging.INFO, ctx, "Registration flow: fallback auth-flow markers detected")
                if _exists(driver, config.reg_signin_anon_selector):
                    if not _click_resilient(driver, config.reg_signin_anon_selector, config.element_wait_seconds, config):
                        if not _navigate_to_signup_fallback(
                            driver,
                            config,
                            logger,
                            ctx,
                            "Sign In Anonymously button not clickable on fallback auth-flow page",
                        ):
                            raise RuntimeError(
                                "Sign In Anonymously button not clickable on fallback auth-flow page; "
                                f"current_url={driver.current_url}"
                            )
                    current_stage = "await_create_account"
                _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account from fallback auth-flow page")
                if not _click_resilient(driver, config.reg_create_account_selector, config.element_wait_seconds, config):
                    raise RuntimeError(
                        "Create Account button not clickable on fallback auth-flow page; "
                        f"current_url={driver.current_url}"
                    )
                if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        "Create-account form was not detectable after fallback auth-flow click; continuing with keyboard fallback",
                    )
                current_stage = "create_account_form"
                break
            if _is_registration_wizard_visible(driver, config):
                _log(logger, logging.INFO, ctx, "Registration flow: wizard already in progress")
                if _exists(driver, config.reg_create_page_marker_selector):
                    current_stage = "create_account_form"
                elif _exists(driver, config.reg_ack_page_marker_selector):
                    current_stage = "acknowledgement"
                elif _exists(driver, config.reg_bday_page_marker_selector):
                    current_stage = "birthday"
                elif _exists(driver, config.reg_gender_page_marker_selector):
                    current_stage = "gender"
                elif _exists(driver, config.reg_meet_page_marker_selector):
                    current_stage = "meet"
                break
            if _is_strong_post_registration_state(driver, config):
                _log(logger, logging.INFO, ctx, "Registration already completed; strong post-registration state detected")
                break
            if _is_chat_ready_state(driver, config):
                if force_strong_completion and not _is_strong_post_registration_state(driver, config):
                    if _has_expected_stage_signal(driver, current_stage, config):
                        _log(
                            logger,
                            logging.WARNING,
                            ctx,
                            (
                                "Premature chat/text-ready UI detected for unregistered profile; "
                                f"continuing registration using expected stage={current_stage}"
                            ),
                        )
                    elif _settle_post_start_stage(driver, current_stage, config):
                        if _exists(driver, config.reg_create_page_marker_selector):
                            current_stage = "create_account_form"
                            _log(
                                logger,
                                logging.INFO,
                                ctx,
                                "Registration flow: create account page appeared after post-start settle window",
                            )
                        elif _exists(driver, config.reg_signin_page_marker_selector) or _exists(
                            driver, config.reg_signin_anon_selector
                        ):
                            _log(logger, logging.INFO, ctx, "Registration flow: sign-in page appeared after settle window")
                            if not _click_resilient(driver, config.reg_signin_anon_selector, config.element_wait_seconds, config):
                                if not _navigate_to_signup_fallback(
                                    driver,
                                    config,
                                    logger,
                                    ctx,
                                    "Sign In Anonymously button not clickable after settle window",
                                ):
                                    raise RuntimeError(
                                        "Sign In Anonymously button not clickable after settle window; "
                                        f"current_url={driver.current_url}"
                                    )
                            current_stage = "await_create_account"
                            _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                            _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account after settle window")
                            if not _click_resilient(
                                driver, config.reg_create_account_selector, config.element_wait_seconds, config
                            ):
                                raise RuntimeError(
                                    "Create Account button not clickable after settle-window sign-in flow; "
                                    f"current_url={driver.current_url}"
                                )
                            if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                                raise RuntimeError(
                                    "Create-account form did not appear after settle-window sign-in flow; "
                                    f"current_url={driver.current_url}"
                                )
                            current_stage = "create_account_form"
                        elif _exists(driver, config.reg_anon_auth_page_marker_selector):
                            _log(logger, logging.INFO, ctx, "Registration flow: anon-auth marker appeared after settle window")
                            _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                            _log(logger, logging.INFO, ctx, "Registration flow: clicking Create Account after settle window")
                            if not _click_resilient(
                                driver, config.reg_create_account_selector, config.element_wait_seconds, config
                            ):
                                raise RuntimeError(
                                    "Create Account button not clickable after settle-window anon-auth state; "
                                    f"current_url={driver.current_url}"
                                )
                            if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                                raise RuntimeError(
                                    "Create-account form did not appear after settle-window anon-auth state; "
                                    f"current_url={driver.current_url}"
                                )
                            current_stage = "create_account_form"
                        elif _is_auth_flow_visible(driver, config):
                            _log(logger, logging.INFO, ctx, "Registration flow: fallback auth-flow markers appeared after settle window")
                            if _exists(driver, config.reg_signin_anon_selector):
                                if not _click_resilient(
                                    driver, config.reg_signin_anon_selector, config.element_wait_seconds, config
                                ):
                                    if not _navigate_to_signup_fallback(
                                        driver,
                                        config,
                                        logger,
                                        ctx,
                                        "Sign In Anonymously button not clickable on fallback auth-flow page after settle window",
                                    ):
                                        raise RuntimeError(
                                            "Sign In Anonymously button not clickable on fallback auth-flow page after settle window; "
                                            f"current_url={driver.current_url}"
                                        )
                                current_stage = "await_create_account"
                            _wait_present(driver, config.reg_create_account_selector, config.element_wait_seconds)
                            _log(
                                logger,
                                logging.INFO,
                                ctx,
                                "Registration flow: clicking Create Account after fallback auth-flow detection",
                            )
                            if not _click_resilient(
                                driver, config.reg_create_account_selector, config.element_wait_seconds, config
                            ):
                                raise RuntimeError(
                                    "Create Account button not clickable on fallback auth-flow page after settle window; "
                                    f"current_url={driver.current_url}"
                                )
                            if not _wait_for_create_account_form_transition(driver, config, logger, ctx):
                                raise RuntimeError(
                                    "Create-account form did not appear on fallback auth-flow page after settle window; "
                                    f"current_url={driver.current_url}"
                                )
                            current_stage = "create_account_form"
                        elif _is_registration_wizard_visible(driver, config):
                            _log(logger, logging.INFO, ctx, "Registration flow: wizard became visible after settle window")
                            if _exists(driver, config.reg_create_page_marker_selector):
                                current_stage = "create_account_form"
                            elif _exists(driver, config.reg_ack_page_marker_selector):
                                current_stage = "acknowledgement"
                            elif _exists(driver, config.reg_bday_page_marker_selector):
                                current_stage = "birthday"
                            elif _exists(driver, config.reg_gender_page_marker_selector):
                                current_stage = "gender"
                            elif _exists(driver, config.reg_meet_page_marker_selector):
                                current_stage = "meet"
                        elif _is_strong_post_registration_state(driver, config):
                            _log(
                                logger,
                                logging.INFO,
                                ctx,
                                "Registration already completed; strong post-registration state detected after settle window",
                            )
                        else:
                            raise RuntimeError(
                                "Premature chat/text-ready UI detected and settle window found no registration progression; "
                                f"stage={current_stage} current_url={driver.current_url}"
                            )
                    else:
                        raise RuntimeError(
                            "Premature chat/text-ready UI detected before strong registration completion proof; "
                            f"stage={current_stage} current_url={driver.current_url}"
                        )
                else:
                    _log(logger, logging.INFO, ctx, "Registration appears already completed; skipping wizard")
                break

            if time.monotonic() >= post_start_deadline:
                raise RuntimeError(
                    "Unable to determine registration state after homepage/start; "
                    f"current_url={driver.current_url}"
                )

            time.sleep(0.5)

        if current_stage == "create_account_form" or _is_create_account_form_visible(driver, config):
            current_stage = "create_account_form"
            _log(logger, logging.INFO, ctx, "Registration flow: create account page detected")
            created = False
            dead_submit_attempts = 0
            for attempt in range(1, config.registration_max_username_attempts + 1):
                username = _gen_username(config)
                password = _gen_password(config)
                use_keyboard_fallback = not _is_create_account_form_visible(driver, config)
                if use_keyboard_fallback:
                    submit_clicked = _submit_create_account_form_via_tab_fallback(
                        driver,
                        username,
                        password,
                        config,
                        logger,
                        ctx,
                    )
                else:
                    _log(logger, logging.INFO, ctx, f"Registration flow: filling username (attempt {attempt})")
                    _set_input(driver, config.reg_username_selector, username, config.element_wait_seconds, config=config)
                    _log(logger, logging.INFO, ctx, "Registration flow: filling password")
                    _set_input(driver, config.reg_password_selector, password, config.element_wait_seconds, config=config)
                    _log(logger, logging.INFO, ctx, "Registration flow: filling confirm password")
                    _set_input(
                        driver,
                        config.reg_confirm_password_selector,
                        password,
                        config.element_wait_seconds,
                        config=config,
                    )
                    _blur_input(driver, config.reg_confirm_password_selector)
                    _action_pause(config)
                    _log(logger, logging.INFO, ctx, "Registration flow: submitting create account form")

                    submit_clicked = _click_resilient(
                        driver,
                        config.reg_submit_selector,
                        config.element_wait_seconds,
                        config,
                    )
                    if not submit_clicked:
                        submit_clicked = _js_click_selector(driver, config.reg_submit_selector)
                if not submit_clicked:
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        f"Create account submit button was not clickable on attempt {attempt}; retrying",
                    )
                    continue

                state_changed = False
                for submit_try in range(1, 3):
                    time.sleep(1)

                    progressed_stage = _post_create_progress_stage(driver, config)
                    if progressed_stage == "acknowledgement":
                        created = True
                        current_stage = "acknowledgement"
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    if progressed_stage == "birthday":
                        created = True
                        current_stage = "birthday"
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    if progressed_stage == "gender":
                        created = True
                        current_stage = "gender"
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    if progressed_stage == "meet":
                        created = True
                        current_stage = "meet"
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    if progressed_stage == "completed":
                        created = True
                        dead_submit_attempts = 0
                        state_changed = True
                        break

                    if _exists(driver, config.reg_error_username_taken_selector):
                        _log(logger, logging.WARNING, ctx, f"Username taken on attempt {attempt}; retrying")
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    if _exists(driver, config.reg_error_short_password_selector):
                        _log(logger, logging.ERROR, ctx, "Registration password rejected by platform")
                        return False
                    if _exists(driver, config.reg_error_password_mismatch_selector):
                        _log(logger, logging.ERROR, ctx, "Password mismatch encountered unexpectedly")
                        return False
                    try:
                        _wait_present(driver, config.reg_ack_page_marker_selector, 2)
                        created = True
                        current_stage = "acknowledgement"
                        dead_submit_attempts = 0
                        state_changed = True
                        break
                    except TimeoutException:
                        pass

                    if submit_try == 1 and _exists(driver, config.reg_create_page_marker_selector):
                        _log(
                            logger,
                            logging.WARNING,
                            ctx,
                            (
                                "Create account submit produced no immediate state change; "
                                "retrying click once before rotating credentials"
                            ),
                        )
                        if not use_keyboard_fallback:
                            if not _click_resilient(
                                driver,
                                config.reg_submit_selector,
                                max(2, config.element_wait_seconds // 2),
                                config,
                            ):
                                break
                        else:
                            break

                if created:
                    break

                if state_changed:
                    continue

                dead_submit_attempts += 1
                _log(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        f"Create account attempt {attempt} produced no validation or page transition; "
                        "treating as non-submission and retrying with new credentials"
                    ),
                )
                if dead_submit_attempts >= config.registration_dead_submit_restart_threshold:
                    raise RetryableRegistrationError(
                        "Create account submit stayed inert across repeated attempts; restarting browser session"
                    )

            if not created:
                _log(logger, logging.ERROR, ctx, "Failed to create account after max username attempts")
                return False

        if current_stage == "acknowledgement" or _post_create_progress_stage(driver, config) == "acknowledgement":
            current_stage = "acknowledgement"
            _log(logger, logging.INFO, ctx, "Registration flow: acknowledgement page detected")
            clicked_ack = False
            progressed_stage = _wait_for_acknowledgement_progress(driver, config, timeout_seconds=0.75)
            if progressed_stage is None:
                click_attempts = (
                    (
                        "resilient selector click",
                        lambda: _click_resilient(
                            driver,
                            config.reg_ack_agree_selector,
                            config.element_wait_seconds,
                            config,
                        ),
                    ),
                    (
                        "JS xpath click",
                        lambda: _js_click_xpath(driver, config.reg_ack_agree_selector),
                    ),
                    (
                        "JS acknowledgement text fallback",
                        lambda: _js_click_acknowledgement_accept(driver),
                    ),
                )
                for attempt_label, click_attempt in click_attempts:
                    if not click_attempt():
                        continue
                    progressed_stage = _wait_for_acknowledgement_progress(
                        driver,
                        config,
                        timeout_seconds=max(1.5, float(config.element_wait_seconds) / 2.0),
                    )
                    if progressed_stage is not None:
                        clicked_ack = True
                        break
                    _log(
                        logger,
                        logging.WARNING,
                        ctx,
                        (
                            f"Registration flow: acknowledgement {attempt_label} did not advance the wizard; "
                            "trying next fallback"
                        ),
                    )
            else:
                clicked_ack = True
            if not clicked_ack and progressed_stage is None:
                body_snippet = " ".join(_body_text(driver).split())[:240]
                raise RuntimeError(
                    "Acknowledgement page detected but I Agree button was not clickable; "
                    f"current_url={driver.current_url} body_snippet={body_snippet!r}"
                )

        if current_stage == "birthday" or _post_create_progress_stage(driver, config) == "birthday":
            current_stage = "birthday"
            _log(logger, logging.INFO, ctx, "Registration flow: birthday step detected")
            _select_birthday(driver, config)
            _click(driver, config.reg_bday_next_selector, config.element_wait_seconds, config=config)

        if current_stage == "gender" or _post_create_progress_stage(driver, config) == "gender":
            current_stage = "gender"
            _log(logger, logging.INFO, ctx, "Registration flow: gender step detected")
            if _exists(driver, config.reg_gender_select_selector):
                _select_dropdown_value(
                    driver,
                    config.reg_gender_select_selector,
                    "f",
                    config.element_wait_seconds,
                    config=config,
                )
            else:
                _click(driver, config.reg_gender_female_selector, config.element_wait_seconds, config=config)
            _click(driver, config.reg_gender_next_selector, config.element_wait_seconds, config=config)

        if current_stage == "meet" or _post_create_progress_stage(driver, config) == "meet":
            current_stage = "meet"
            _log(logger, logging.INFO, ctx, "Registration flow: meet step detected")
            if _exists(driver, config.reg_meet_select_selector):
                _select_dropdown_value(
                    driver,
                    config.reg_meet_select_selector,
                    "a",
                    config.element_wait_seconds,
                    config=config,
                )
            else:
                _click(driver, config.reg_meet_everyone_selector, config.element_wait_seconds, config=config)
            _click(driver, config.reg_meet_start_selector, config.element_wait_seconds, config=config)
            _log(
                logger,
                logging.INFO,
                ctx,
                (
                    "Registration flow: waiting for final transition after last Start "
                    f"(timeout={config.registration_final_transition_timeout_seconds}s)"
                ),
            )
            if not _wait_for_strong_post_registration_state(
                driver,
                config,
                timeout_seconds=config.registration_final_transition_timeout_seconds,
            ):
                grace_seconds = max(8.0, float(config.element_wait_seconds))
                _log(
                    logger,
                    logging.WARNING,
                    ctx,
                    (
                        "Registration flow: final video transition exceeded initial timeout; "
                        f"waiting an extra {grace_seconds:.1f}s grace window before recovery"
                    ),
                )
                _wait_for_strong_post_registration_state(
                    driver,
                    config,
                    timeout_seconds=grace_seconds,
                )
            if force_strong_completion and not _is_strong_post_registration_state(driver, config):
                raise RuntimeError(
                    "Registration finished wizard steps but did not reach strong completion proof (/video); "
                    f"current_url={driver.current_url}"
                )

        wizard_clear = False
        retries = max(1, config.registration_wizard_visible_retries)
        for attempt in range(1, retries + 1):
            if not _is_registration_wizard_visible(driver, config):
                wizard_clear = True
                break
            if _is_post_registration_success_state(driver, config):
                wizard_clear = True
                break
            if attempt < retries:
                low = min(
                    config.registration_wizard_retry_delay_min_seconds,
                    config.registration_wizard_retry_delay_max_seconds,
                )
                high = max(
                    config.registration_wizard_retry_delay_min_seconds,
                    config.registration_wizard_retry_delay_max_seconds,
                )
                time.sleep(random.uniform(max(0.0, low), max(0.0, high)))

        if not wizard_clear:
            _log(
                logger,
                logging.WARNING,
                ctx,
                "Registration flow: wizard still visible after retries; trying recovery navigation",
            )
            driver.get(config.registration_home_url)
            _action_pause(config)
            if _is_strong_post_registration_state(driver, config):
                wizard_clear = True
                _log(logger, logging.INFO, ctx, "Registration flow: recovered after homepage reload")
            else:
                _log(
                    logger,
                    logging.INFO,
                    ctx,
                    "Registration flow: homepage reached without video proof; clicking Start to verify registration state",
                )
                start_clicked = _click_if_present(driver, config.reg_start_selector, timeout=6, config=config)
                if start_clicked:
                    _log(logger, logging.INFO, ctx, "Registration flow: start button clicked during final recovery verification")
                    if _wait_for_strong_post_registration_state(
                        driver,
                        config,
                        timeout_seconds=max(12.0, float(config.element_wait_seconds) * 2.0),
                    ):
                        wizard_clear = True
                        _log(logger, logging.INFO, ctx, "Registration flow: recovered after homepage start verification")
                    elif (
                        _is_signin_state(driver, config)
                        or _exists(driver, config.reg_anon_auth_page_marker_selector)
                        or _is_auth_flow_visible(driver, config)
                        or _is_registration_wizard_visible(driver, config)
                    ):
                        raise RetryableRegistrationError(
                            "Homepage recovery verification returned to auth/registration flow; restarting registration attempt"
                        )
                elif (
                    _is_signin_state(driver, config)
                    or _exists(driver, config.reg_anon_auth_page_marker_selector)
                    or _is_auth_flow_visible(driver, config)
                    or _is_registration_wizard_visible(driver, config)
                ):
                    raise RetryableRegistrationError(
                        "Homepage recovery landed on auth/registration flow; restarting registration attempt"
                    )

        if not wizard_clear:
            raise RuntimeError(
                "Registration wizard did not complete expected transitions after retries and recovery; "
                f"current_url={driver.current_url}"
            )

        if not _is_video_success_state(driver, config):
            raise RuntimeError(
                "Registration lost track of wizard progression and did not reach the expected video success state; "
                f"current_url={driver.current_url}"
            )

        _log(
            logger,
            logging.INFO,
            ctx,
            "Registration flow: video state detected after signup; configuring geo preferences",
        )
        _configure_geo_preferences_best_effort(driver, config, logger, ctx)

        driver.get(config.target_url)
        _log(logger, logging.INFO, ctx, "Registration flow completed; redirected to text URL")
        return True

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        if browser_started:
            try:
                client.stop_browser(user_id)
            except Exception:  # noqa: BLE001
                pass


def run_registration(
    user_id: str,
    config: BotConfig,
    logger: logging.Logger,
    ctx: RuntimeContext,
    stop_event: Event,
) -> bool:
    max_retryable_restarts = max(0, config.registration_retryable_restart_attempts)

    for attempt in range(1, max_retryable_restarts + 2):
        try:
            return _run_registration_once(user_id, config, logger, ctx, stop_event)
        except RetryableRegistrationError as error:
            if stop_event.is_set():
                _log(logger, logging.WARNING, ctx, f"Registration interrupted during retryable failure: {error}")
                return False
            if attempt > max_retryable_restarts:
                _log(logger, logging.ERROR, ctx, f"Registration flow failed: {error}")
                return False
            _log(
                logger,
                logging.WARNING,
                ctx,
                (
                    f"Registration flow hit retryable browser-state failure "
                    f"({attempt}/{max_retryable_restarts + 1}): {error}"
                ),
            )
            time.sleep(config.restart_backoff_seconds * attempt)
        except Exception as error:  # noqa: BLE001
            _log(logger, logging.ERROR, ctx, f"Registration flow failed: {error}")
            return False

    return False
