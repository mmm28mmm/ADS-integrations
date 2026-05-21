from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ROOT_DIR = Path(__file__).resolve().parent
THUNDRBOT_DIR = ROOT_DIR / "thundrbot"
if str(THUNDRBOT_DIR) not in sys.path:
    sys.path.insert(0, str(THUNDRBOT_DIR))

from thundr_bot.adspower_client import AdsPowerClient  # noqa: E402


ADSPOWER_API_BASE = os.getenv("ADSPOWER_API_BASE", "http://local.adspower.net:50325")
ADSPOWER_API_KEY = os.getenv("ADSPOWER_API_KEY") or None
ADSPOWER_GROUP_ID = os.getenv("ADSPOWER_GROUP_ID", "7037614")

REDDIT_BASE_URL = "https://www.reddit.com"
OLD_REDDIT_BASE_URL = "https://old.reddit.com"
REDDIT_LOGIN_URL = f"{REDDIT_BASE_URL}/login/"
DEFAULT_WAIT_SECONDS = 20
DEFAULT_DELETE_WAIT_SECONDS = 12
BROWSER_WINDOW_WIDTH = 1450
BROWSER_WINDOW_HEIGHT = 950
RUN_REPORT_DIR = ROOT_DIR / "logs" / "reddit_cleanup_reports"
FAILURE_SCREENSHOT_DIR = ROOT_DIR / "logs" / "reddit_cleanup_failure_screenshots"
SESSION_LOG_DIR = ROOT_DIR / "logs" / "reddit_cleanup_sessions"
ADSPOWER_MIN_CALL_INTERVAL_SECONDS = float(os.getenv("ADSPOWER_MIN_CALL_INTERVAL_SECONDS", "1.25"))
ADSPOWER_RATE_LIMIT_RETRIES = int(os.getenv("ADSPOWER_RATE_LIMIT_RETRIES", "6"))
REDDIT_ACCOUNT_RETRY_ATTEMPTS = int(os.getenv("REDDIT_ACCOUNT_RETRY_ATTEMPTS", "3"))
REDDIT_RETRY_BACKOFF_SECONDS = float(os.getenv("REDDIT_RETRY_BACKOFF_SECONDS", "4"))
AUTOMATION_TAB_NAME = "__reddit_cleanup__"

PRINT_LOCK = threading.Lock()
ADSPOWER_API_LOCK = threading.Lock()
ADSPOWER_LAST_CALL_AT = 0.0

LOGIN_USERNAME_SELECTORS = (
    "css=input[name='username'][autocomplete='username webauthn']",
    "css=input[name='username']",
    "css=input[autocomplete='username webauthn']",
    "xpath=//input[@name='username' and @type='text']",
    "xpath=//input[contains(@placeholder, 'Email or username')]",
    "xpath=//input[contains(@aria-label, 'Email or username')]",
)

LOGIN_PASSWORD_SELECTORS = (
    "css=input[name='password'][autocomplete='current-password']",
    "css=input[name='password']",
    "css=input[autocomplete='current-password']",
    "css=input[type='password']",
    "xpath=//input[@name='password' and @type='password']",
    "xpath=//input[contains(@placeholder, 'Password')]",
    "xpath=//input[contains(@aria-label, 'Password')]",
)

LOGIN_BUTTON_SELECTORS = (
    "css=button.login[type='button']",
    "xpath=//button[contains(@class, 'login') and @type='button']",
    "xpath=//button[@type='button' and .//span[normalize-space()='Log In']]",
    "xpath=//button[@type='button' and .//*[normalize-space()='Log In']]",
    "xpath=//button[normalize-space()='Log In' or normalize-space()='Log in']",
)

DELETE_BUTTON_SELECTORS = (
    "css=a.togglebutton[data-event-action='delete']",
    "xpath=.//a[@data-event-action='delete' and normalize-space()='delete']",
    "xpath=.//a[contains(@class, 'togglebutton') and normalize-space()='delete']",
)

DELETE_CONFIRM_SELECTORS = (
    "css=span.option.error.active a.yes",
    "xpath=.//span[contains(@class, 'option') and contains(@class, 'error') and contains(@class, 'active')]//a[contains(@class, 'yes')]",
    "xpath=.//a[contains(@class, 'yes') and normalize-space()='yes']",
)

NEXT_PAGE_SELECTORS = (
    "css=a[rel='nofollow next']",
    "css=span.next-button a",
    "xpath=//a[@rel='nofollow next' and contains(normalize-space(), 'next')]",
)


@dataclass(frozen=True)
class RedditCredentials:
    username: str
    password: str


@dataclass(frozen=True)
class RedditAdsPowerConfig:
    api_base: str = ADSPOWER_API_BASE
    api_key: str | None = ADSPOWER_API_KEY
    api_timeout_seconds: int = 20
    api_retries: int = 3
    group_id: str = ADSPOWER_GROUP_ID
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    delete_wait_seconds: int = DEFAULT_DELETE_WAIT_SECONDS
    extension_category_id: str | None = None
    extension_category_name: str | None = None
    disable_extensions_on_launch: bool = False


@dataclass(frozen=True)
class AccountResult:
    username: str
    status: str
    posts_deleted: int
    profile_id: str | None
    original_input: str | None = None
    error: str | None = None
    screenshot_path: str | None = None
    finished_at_utc: str | None = None


@dataclass(frozen=True)
class ExtensionCategoryVerification:
    requested_category_id: str | None
    actual_category_id: str | None
    actual_category_name: str | None


@dataclass(frozen=True)
class RedditRuntimeContext:
    run_id: str
    user_id: str


class RedditChallengeError(RuntimeError):
    """Raised when Reddit requires verification this script should not solve."""


class RedditTransientError(RuntimeError):
    """Raised for transient Reddit/proxy/browser failures worth retrying."""


def safe_print(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def script_timestamp_label() -> str:
    return datetime.fromtimestamp(Path(__file__).stat().st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def setup_logger(ctx: RedditRuntimeContext) -> logging.Logger:
    SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger_name = f"reddit_cleanup_{ctx.run_id}_{ctx.user_id}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | run=%(run_id)s | user=%(user_id)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(
            SESSION_LOG_DIR / f"{safe_filename(ctx.user_id)}.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger


def log_ctx(logger: logging.Logger, level: int, ctx: RedditRuntimeContext, message: str) -> None:
    logger.log(level, message, extra={"run_id": ctx.run_id, "user_id": ctx.user_id})


def page_diagnostics(driver: webdriver.Chrome) -> str:
    try:
        current_url = (driver.current_url or "").strip()
    except WebDriverException:
        current_url = "<unavailable>"
    try:
        title = (driver.title or "").strip()
    except WebDriverException:
        title = "<unavailable>"
    snippet = body_text(driver)[:220] or "<empty>"
    return f"url={current_url} | title={title or '<empty>'} | body={snippet}"


def active_element_summary(driver: webdriver.Chrome) -> str:
    try:
        payload = driver.execute_script(
            """
            const el = document.activeElement;
            if (!el) {
              return { tag: '', type: '', name: '', placeholder: '', className: '', valueLength: 0 };
            }
            return {
              tag: (el.tagName || '').toLowerCase(),
              type: el.getAttribute('type') || '',
              name: el.getAttribute('name') || '',
              placeholder: el.getAttribute('placeholder') || '',
              className: el.className || '',
              valueLength: (el.value || '').length,
            };
            """
        )
    except WebDriverException:
        return "<unavailable>"

    if not isinstance(payload, dict):
        return "<unknown>"
    return (
        f"tag={payload.get('tag','')} type={payload.get('type','')} "
        f"name={payload.get('name','')} placeholder={payload.get('placeholder','')} "
        f"valueLength={payload.get('valueLength',0)}"
    )


def login_form_value_lengths(driver: webdriver.Chrome) -> str:
    script = """
        function collectRoots(root, roots) {
          if (!root || roots.includes(root)) {
            return;
          }
          roots.push(root);
          const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
          for (const element of elements) {
            if (element.shadowRoot) {
              collectRoots(element.shadowRoot, roots);
            }
          }
        }

        const roots = [];
        collectRoots(document, roots);
        const result = { usernameLength: null, passwordLength: null, buttonDisabled: null };

        for (const root of roots) {
          const username = root.querySelector("input[name='username'], input[autocomplete='username webauthn']");
          if (username && result.usernameLength === null) {
            result.usernameLength = (username.value || '').length;
          }

          const password = root.querySelector("input[name='password'], input[autocomplete='current-password'], input[type='password']");
          if (password && result.passwordLength === null) {
            result.passwordLength = (password.value || '').length;
          }

          const button = root.querySelector("button.login[type='button'], button[type='button']");
          if (button && result.buttonDisabled === null) {
            result.buttonDisabled = !!button.disabled;
          }
        }
        return result;
    """
    try:
        payload = driver.execute_script(script)
    except WebDriverException:
        return "<unavailable>"
    if not isinstance(payload, dict):
        return "<unknown>"
    return (
        f"usernameLength={payload.get('usernameLength')} "
        f"passwordLength={payload.get('passwordLength')} "
        f"buttonDisabled={payload.get('buttonDisabled')}"
    )


def is_adspower_rate_limit_error(error: Exception | str) -> bool:
    lowered = str(error).lower()
    return (
        "too many request per second" in lowered
        or "too many requests" in lowered
        or "rate limit" in lowered
    )


def run_adspower_call(
    action_name: str,
    func: Any,
    *args: Any,
    retries: int | None = None,
    **kwargs: Any,
) -> Any:
    global ADSPOWER_LAST_CALL_AT

    retries = ADSPOWER_RATE_LIMIT_RETRIES if retries is None else max(1, retries)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            with ADSPOWER_API_LOCK:
                now = time.monotonic()
                wait_seconds = ADSPOWER_MIN_CALL_INTERVAL_SECONDS - (now - ADSPOWER_LAST_CALL_AT)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                result = func(*args, **kwargs)
                ADSPOWER_LAST_CALL_AT = time.monotonic()
                return result
        except Exception as error:  # noqa: BLE001
            last_error = error
            if not is_adspower_rate_limit_error(error) or attempt >= retries:
                raise RuntimeError(f"AdsPower {action_name} failed: {error}") from error
            backoff_seconds = min(2.0 * attempt, 10.0)
            safe_print(
                f"[adspower] {action_name} hit a rate limit, retrying in {backoff_seconds:.1f}s "
                f"({attempt}/{retries})..."
            )
            time.sleep(backoff_seconds)

    assert last_error is not None
    raise RuntimeError(f"AdsPower {action_name} failed: {last_error}") from last_error


def parse_credentials(raw_value: str) -> RedditCredentials:
    raw_value = raw_value.strip()
    parts = raw_value.split()
    if len(parts) != 2:
        raise ValueError("Credentials must use 'reddit_username password' format")

    username, password = parts

    if not username:
        raise ValueError("The Reddit username is missing")
    if not password:
        raise ValueError("The Reddit password is missing")
    return RedditCredentials(username=username, password=password)


def prompt_concurrency() -> int:
    while True:
        raw_value = input("Concurrent browser instances: ").strip()
        if raw_value.isdigit() and int(raw_value) > 0:
            return int(raw_value)
        print("Invalid value. Enter a whole number greater than 0.")


def prompt_credentials() -> list[RedditCredentials]:
    safe_print("")
    safe_print("Paste Reddit credentials in this format:")
    safe_print("reddit_username password")
    safe_print("One account per line. Blank line when finished.")
    safe_print("")

    credentials: list[RedditCredentials] = []
    seen_usernames: set[str] = set()

    while True:
        try:
            raw_line = input().strip()
        except EOFError:
            break
        if not raw_line:
            break
        try:
            parsed = parse_credentials(raw_line)
        except ValueError as error:
            safe_print(f"Skipping invalid input: {error}")
            continue
        lowered = parsed.username.lower()
        if lowered in seen_usernames:
            safe_print(f"Skipping duplicate username: {parsed.username}")
            continue
        seen_usernames.add(lowered)
        credentials.append(parsed)

    return credentials


def prompt_extension_category_override() -> tuple[str | None, str | None]:
    safe_print("")
    safe_print(
        "Extension category override: press Enter to use AdsPower's default/team extensions."
    )
    safe_print(
        "To test with no extensions, enter the name or ID of an empty AdsPower extension category."
    )
    raw_value = input("Extension category name or ID (optional): ").strip()
    if not raw_value:
        return None, None
    if raw_value.isdigit():
        return raw_value, None
    return None, raw_value


def prompt_disable_extensions_on_launch() -> bool:
    while True:
        raw_value = input("Disable all browser extensions on launch? (y/N): ").strip().lower()
        if raw_value in {"", "n", "no"}:
            return False
        if raw_value in {"y", "yes"}:
            return True
        print("Please answer y or n.")


def build_profile_payload(credentials: RedditCredentials, config: RedditAdsPowerConfig) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_username = safe_filename(credentials.username)
    payload = {
        "name": f"reddit_cleanup_{safe_username}_{timestamp}",
        "group_id": config.group_id,
        "remark": f"REDDIT CLEANUP {credentials.username} {timestamp}",
        "platform": "reddit.com",
        "username": credentials.username,
        "password": credentials.password,
        "proxyid": "random",
        "fingerprint_config": {
            "automatic_timezone": 1,
            "language": ["en-US", "en"],
            "flash": "block",
            "fonts": ["all"],
            "webrtc": "disabled",
            "random_ua": {
                "ua_browser": ["chrome"],
                "ua_system_version": ["Windows 10", "Windows 11"],
            },
            "screen_resolution": f"{BROWSER_WINDOW_WIDTH}_{BROWSER_WINDOW_HEIGHT}",
            "browser_kernel_config": {"type": "chrome", "version": "ua_auto"},
        },
    }
    if config.extension_category_id:
        payload["sys_app_cate_id"] = config.extension_category_id
    if config.disable_extensions_on_launch:
        payload["launch_args"] = ["--disable-extensions"]
    return payload


def resolve_extension_category_override(
    client: AdsPowerClient,
    config: RedditAdsPowerConfig,
) -> RedditAdsPowerConfig:
    if config.extension_category_id or config.extension_category_name:
        resolved_id = run_adspower_call(
            "resolve_extension_category_id",
            client.resolve_extension_category_id,
            category_id=config.extension_category_id,
            category_name=config.extension_category_name,
            retries=3,
        )
        resolved_id = str(resolved_id or "").strip() or None
        if not resolved_id:
            raise RuntimeError("AdsPower did not resolve the requested extension category")
        return RedditAdsPowerConfig(
            api_base=config.api_base,
            api_key=config.api_key,
            api_timeout_seconds=config.api_timeout_seconds,
            api_retries=config.api_retries,
            group_id=config.group_id,
            wait_seconds=config.wait_seconds,
            delete_wait_seconds=config.delete_wait_seconds,
            extension_category_id=resolved_id,
            extension_category_name=config.extension_category_name,
            disable_extensions_on_launch=config.disable_extensions_on_launch,
        )
    return config


def verify_profile_extension_category(
    client: AdsPowerClient,
    profile_id: str,
    requested_category_id: str | None,
) -> ExtensionCategoryVerification:
    profile = run_adspower_call("get_profile", client.get_profile, profile_id, retries=3)
    actual_category_id = str(profile.get("category_id") or profile.get("sys_app_cate_id") or "").strip() or None
    actual_category_name = str(profile.get("category_name") or "").strip() or None
    return ExtensionCategoryVerification(
        requested_category_id=str(requested_category_id or "").strip() or None,
        actual_category_id=actual_category_id,
        actual_category_name=actual_category_name,
    )


def create_profile_with_random_saved_proxy(
    client: AdsPowerClient,
    credentials: RedditCredentials,
    config: RedditAdsPowerConfig,
) -> str:
    payload = build_profile_payload(credentials, config)
    try:
        return run_adspower_call("create_profile", client.create_profile, payload)
    except RuntimeError as first_error:
        fallback_payload = dict(payload)
        fallback_payload.pop("proxyid", None)
        try:
            profile_id = run_adspower_call("create_profile_fallback", client.create_profile, fallback_payload)
        except Exception as fallback_error:  # noqa: BLE001
            raise RuntimeError(
                "AdsPower could not create a temporary Reddit profile with saved proxies. "
                f"Initial create error: {first_error}; fallback create error: {fallback_error}"
            ) from fallback_error
        try:
            run_adspower_call("assign_saved_proxy", client.update_profile_saved_proxy, profile_id, "random")
            if config.extension_category_id:
                run_adspower_call(
                    "apply_extension_category_after_create",
                    client.update_profile_extension_category,
                    profile_id,
                    config.extension_category_id,
                    retries=3,
                )
            return profile_id
        except Exception as proxy_error:  # noqa: BLE001
            try:
                run_adspower_call("delete_profile_after_proxy_failure", client.delete_profiles, [profile_id], retries=3)
            except Exception:
                pass
            raise RuntimeError(
                "Created the profile, but a follow-up AdsPower profile update failed. "
                f"Initial create error: {first_error}; follow-up update error: {proxy_error}"
            ) from proxy_error

    if config.extension_category_id:
        run_adspower_call(
            "apply_extension_category_after_create",
            client.update_profile_extension_category,
            profile_id,
            config.extension_category_id,
            retries=3,
        )
        verification = verify_profile_extension_category(client, profile_id, config.extension_category_id)
        safe_print(
            f"[{credentials.username}] AdsPower stored extension category "
            f"{verification.actual_category_id or 'none'}"
            + (f" ({verification.actual_category_name})" if verification.actual_category_name else "")
        )
        if verification.actual_category_id != verification.requested_category_id:
            try:
                run_adspower_call(
                    "update_profile_remark_on_extension_mismatch",
                    client.update_profile_remark,
                    profile_id,
                    (
                        f"REDDIT CLEANUP EXTENSION MISMATCH "
                        f"requested={verification.requested_category_id or 'none'} "
                        f"actual={verification.actual_category_id or 'none'}"
                    ),
                )
            except Exception:
                pass
            raise RuntimeError(
                "AdsPower did not persist the requested extension category. "
                f"requested={verification.requested_category_id or 'none'} "
                f"actual={verification.actual_category_id or 'none'} "
                f"name={verification.actual_category_name or '<unknown>'}"
            )
    return profile_id


def locator(selector: str) -> tuple[str, str]:
    if selector.startswith("xpath="):
        return By.XPATH, selector.removeprefix("xpath=")
    if selector.startswith("css="):
        return By.CSS_SELECTOR, selector.removeprefix("css=")
    return By.CSS_SELECTOR, selector


def wait_visible(driver: webdriver.Chrome, selector: str, timeout: int) -> WebElement:
    return WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(locator(selector)))


def wait_clickable(driver: webdriver.Chrome, selector: str, timeout: int) -> WebElement:
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(locator(selector)))


def exists(driver: webdriver.Chrome, selector: str, timeout: float = 1.0) -> bool:
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator(selector)))
        return True
    except TimeoutException:
        return False
    except WebDriverException:
        return False


def normalize_browser_window(driver: webdriver.Chrome) -> None:
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id is not None:
            driver.execute_cdp_cmd(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "left": 20,
                        "top": 20,
                        "width": BROWSER_WINDOW_WIDTH,
                        "height": BROWSER_WINDOW_HEIGHT,
                        "windowState": "normal",
                    },
                },
            )
    except Exception:
        pass


def attach_driver(browser_session: Any) -> webdriver.Chrome:
    options = Options()
    options.add_experimental_option("debuggerAddress", browser_session.debugger_address)
    service = Service(executable_path=browser_session.webdriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.set_window_rect(x=20, y=20, width=BROWSER_WINDOW_WIDTH, height=BROWSER_WINDOW_HEIGHT)
    except WebDriverException:
        try:
            driver.set_window_size(BROWSER_WINDOW_WIDTH, BROWSER_WINDOW_HEIGHT)
        except WebDriverException:
            pass
    normalize_browser_window(driver)
    return driver


def _find_automation_tab_handle(driver: webdriver.Chrome) -> str | None:
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            tab_name = driver.execute_script("return window.name || '';")
            if tab_name == AUTOMATION_TAB_NAME:
                return handle
        except WebDriverException:
            continue
    return None


def open_reddit_tab(
    driver: webdriver.Chrome,
    url: str,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    automation_handle = _find_automation_tab_handle(driver)
    if automation_handle is not None:
        driver.switch_to.window(automation_handle)
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, f"Reusing automation tab for {url}")
    else:
        try:
            driver.switch_to.new_window("tab")
        except WebDriverException:
            driver.execute_script("window.open('about:blank', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
        driver.execute_script("window.name = arguments[0];", AUTOMATION_TAB_NAME)
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, f"Created automation tab for {url}")

    driver.get(url)
    normalize_browser_window(driver)
    WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
    )
    try:
        driver.execute_script("window.name = arguments[0];", AUTOMATION_TAB_NAME)
    except WebDriverException:
        pass
    if url == REDDIT_LOGIN_URL:
        force_canonical_reddit_login(driver, logger=logger, ctx=ctx)
    if logger is not None and ctx is not None:
        log_ctx(logger, logging.INFO, ctx, f"Navigation complete: {page_diagnostics(driver)}")


def body_text(driver: webdriver.Chrome) -> str:
    try:
        return " ".join((driver.find_element(By.TAG_NAME, "body").text or "").split())
    except WebDriverException:
        return ""


def is_reddit_login_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    return hostname.endswith("reddit.com") and path == "/login"


def is_canonical_reddit_login_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    return (
        hostname == "www.reddit.com"
        and path == "/login"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def force_canonical_reddit_login(
    driver: webdriver.Chrome,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    current_url = (driver.current_url or "").strip()
    if is_canonical_reddit_login_url(current_url):
        return

    if logger is not None and ctx is not None:
        log_ctx(
            logger,
            logging.INFO,
            ctx,
            f"Forcing canonical Reddit login URL from {current_url or '<empty>'}",
        )

    for attempt in range(1, 4):
        try:
            driver.get("about:blank")
            WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
                lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        driver.get(REDDIT_LOGIN_URL)
        WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
            lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
        )
        normalize_browser_window(driver)

        current_url = (driver.current_url or "").strip()
        if is_canonical_reddit_login_url(current_url):
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Canonical Reddit login confirmed on attempt {attempt}")
            return

        if logger is not None and ctx is not None:
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                f"Canonical login not reached yet on attempt {attempt}: {current_url or '<empty>'}",
            )

    raise RuntimeError(
        f"Could not force the automation tab onto the canonical Reddit login page. Last URL: {current_url or '<empty>'}"
    )


def set_input(driver: webdriver.Chrome, selector: str, value: str, timeout: int) -> None:
    field = wait_visible(driver, selector, timeout)
    set_input_element(driver, field, value, selector)


def set_input_element(
    driver: webdriver.Chrome,
    field: WebElement,
    value: str,
    field_name: str = "input",
) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", field)
    driver.execute_script("arguments[0].focus();", field)
    try:
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(Keys.BACKSPACE)
        field.send_keys(value)
    except WebDriverException:
        pass

    current_value = (field.get_attribute("value") or "").strip()
    if current_value == value:
        return

    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        element.removeAttribute('disabled');
        element.removeAttribute('aria-disabled');
        element.focus();
        element.value = '';
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.value = value;
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
        """,
        field,
        value,
    )

    current_value = (field.get_attribute("value") or "").strip()
    if current_value != value:
        raise RuntimeError(f"Could not set input value for {field_name!r}")


def find_first_visible(driver: webdriver.Chrome, selectors: tuple[str, ...], timeout: int) -> WebElement:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            return wait_visible(driver, selector, timeout)
        except Exception as error:  # noqa: BLE001
            last_error = error
    raise RuntimeError(f"Could not find a visible element for selectors {selectors!r}: {last_error}")


def find_first_present(driver: webdriver.Chrome, selectors: tuple[str, ...], timeout: int) -> WebElement:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            return WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator(selector)))
        except Exception as error:  # noqa: BLE001
            last_error = error
    raise RuntimeError(f"Could not find an element for selectors {selectors!r}: {last_error}")


def find_first_interactable(driver: webdriver.Chrome, selectors: tuple[str, ...], timeout: int) -> WebElement:
    deadline = time.monotonic() + max(1, timeout)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                by, value = locator(selector)
                for element in driver.find_elements(by, value):
                    if not element.is_displayed() or not element.is_enabled():
                        continue
                    return element
            except Exception as error:  # noqa: BLE001
                last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"Could not find an interactable element for selectors {selectors!r}: {last_error}")


def find_first_present_deep(driver: webdriver.Chrome, selectors: tuple[str, ...]) -> WebElement | None:
    script = """
        const selectors = arguments[0];

        function collectRoots(root, roots) {
          if (!root || roots.includes(root)) {
            return;
          }
          roots.push(root);
          const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
          for (const element of elements) {
            if (element.shadowRoot) {
              collectRoots(element.shadowRoot, roots);
            }
          }
        }

        const roots = [];
        collectRoots(document, roots);

        for (const selector of selectors) {
          if (selector.startsWith('xpath=')) {
            continue;
          }
          const cssSelector = selector.startsWith('css=') ? selector.slice(4) : selector;
          for (const root of roots) {
            try {
              const element = root.querySelector(cssSelector);
              if (element) {
                return element;
              }
            } catch (error) {
              // ignore bad selectors for this root
            }
          }
        }
        return null;
    """
    try:
        result = driver.execute_script(script, list(selectors))
    except WebDriverException:
        return None
    return result if isinstance(result, WebElement) else None


def find_first_interactable_deep(driver: webdriver.Chrome, selectors: tuple[str, ...], timeout: int) -> WebElement:
    deadline = time.monotonic() + max(1, timeout)
    last_error: str | None = None
    while time.monotonic() < deadline:
        element = find_first_present_deep(driver, selectors)
        if element is not None:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
                last_error = "element found but not interactable yet"
            except WebDriverException as error:
                last_error = str(error)
        time.sleep(0.2)
    raise RuntimeError(
        f"Could not find an interactable deep DOM element for selectors {selectors!r}: {last_error}"
    )


def _cdp_attributes_to_dict(attributes: list[str] | None) -> dict[str, str]:
    values = attributes or []
    return {
        str(values[index]): str(values[index + 1])
        for index in range(0, max(0, len(values) - 1), 2)
    }


def _cdp_walk_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    stack = [node]
    nodes: list[dict[str, Any]] = []
    while stack:
        current = stack.pop()
        nodes.append(current)
        for key in ("children", "shadowRoots", "contentDocument"):
            value = current.get(key)
            if isinstance(value, list):
                stack.extend(reversed([item for item in value if isinstance(item, dict)]))
            elif isinstance(value, dict):
                stack.append(value)
    return nodes


def _cdp_find_login_node_ids(driver: webdriver.Chrome) -> tuple[int | None, int | None, int | None]:
    document = driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})
    root = document.get("root") or {}
    username_node_id: int | None = None
    password_node_id: int | None = None
    login_button_node_id: int | None = None

    for node in _cdp_walk_nodes(root):
        node_name = str(node.get("nodeName") or "").upper()
        attributes = _cdp_attributes_to_dict(node.get("attributes"))
        node_id = int(node.get("nodeId") or 0)
        if not node_id:
            continue

        if node_name == "INPUT":
            name_value = attributes.get("name", "")
            autocomplete_value = attributes.get("autocomplete", "")
            type_value = attributes.get("type", "")
            if username_node_id is None and name_value == "username":
                username_node_id = node_id
            elif (
                username_node_id is None
                and type_value == "text"
                and "username" in autocomplete_value
            ):
                username_node_id = node_id

            if password_node_id is None and name_value == "password":
                password_node_id = node_id
            elif (
                password_node_id is None
                and type_value == "password"
                and autocomplete_value == "current-password"
            ):
                password_node_id = node_id

        if node_name == "BUTTON" and login_button_node_id is None:
            class_value = attributes.get("class", "")
            type_value = attributes.get("type", "")
            if "login" in class_value and type_value == "button":
                login_button_node_id = node_id

        if username_node_id and password_node_id and login_button_node_id:
            break

    return username_node_id, password_node_id, login_button_node_id


def _cdp_resolve_object_id(driver: webdriver.Chrome, node_id: int) -> str:
    resolved = driver.execute_cdp_cmd("DOM.resolveNode", {"nodeId": node_id})
    object_data = resolved.get("object") or {}
    object_id = str(object_data.get("objectId") or "").strip()
    if not object_id:
        raise RuntimeError(f"CDP could not resolve node object for node_id={node_id}")
    return object_id


def _cdp_set_value(driver: webdriver.Chrome, node_id: int, value: str) -> None:
    object_id = _cdp_resolve_object_id(driver, node_id)
    driver.execute_cdp_cmd(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": """
                function(value) {
                  this.focus();
                  this.value = '';
                  this.dispatchEvent(new Event('input', { bubbles: true }));
                  this.value = value;
                  this.dispatchEvent(new Event('input', { bubbles: true }));
                  this.dispatchEvent(new Event('change', { bubbles: true }));
                }
            """,
            "arguments": [{"value": value}],
        },
    )


def _cdp_click(driver: webdriver.Chrome, node_id: int) -> None:
    object_id = _cdp_resolve_object_id(driver, node_id)
    driver.execute_cdp_cmd(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": """
                function() {
                  this.scrollIntoView({ block: 'center' });
                  this.click();
                }
            """,
            "arguments": [],
        },
    )


def _cdp_focus(driver: webdriver.Chrome, node_id: int) -> None:
    object_id = _cdp_resolve_object_id(driver, node_id)
    driver.execute_cdp_cmd(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": """
                function() {
                  this.scrollIntoView({ block: 'center' });
                  this.focus();
                  this.click();
                }
            """,
            "arguments": [],
        },
    )


def login_to_reddit_via_keyboard(
    driver: webdriver.Chrome,
    credentials: RedditCredentials,
    timeout: int,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    deadline = time.monotonic() + max(1, timeout)
    last_error: str | None = None

    while time.monotonic() < deadline:
        current_url = (driver.current_url or "").strip()
        lowered_url = current_url.lower()
        if "reddit.com/login" not in lowered_url:
            time.sleep(0.25)
            continue
        if not is_canonical_reddit_login_url(current_url):
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Keyboard fallback re-canonicalizing login URL: {current_url}")
            force_canonical_reddit_login(driver, logger=logger, ctx=ctx)
            time.sleep(0.25)
            continue

        block_reason = detect_reddit_block(driver)
        if block_reason:
            raise RedditChallengeError(block_reason)

        try:
            username_node_id, password_node_id, login_button_node_id = _cdp_find_login_node_ids(driver)
            if not username_node_id or not password_node_id or not login_button_node_id:
                last_error = (
                    f"username_node_id={username_node_id}, "
                    f"password_node_id={password_node_id}, "
                    f"login_button_node_id={login_button_node_id}"
                )
                if logger is not None and ctx is not None:
                    log_ctx(logger, logging.INFO, ctx, f"Keyboard login lookup pending: {last_error}")
                time.sleep(0.25)
                continue

            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    (
                        "Keyboard login nodes found: "
                        f"username={username_node_id} password={password_node_id} button={login_button_node_id}"
                    ),
                )

            _cdp_focus(driver, username_node_id)
            time.sleep(0.3)
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Focused username field: {active_element_summary(driver)}")

            active = driver.switch_to.active_element
            active.send_keys(Keys.CONTROL, "a")
            active.send_keys(Keys.BACKSPACE)
            active.send_keys(credentials.username)
            time.sleep(0.4)
            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"Typed username via keyboard: {login_form_value_lengths(driver)} | active={active_element_summary(driver)}",
                )

            _cdp_focus(driver, password_node_id)
            time.sleep(0.4)
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Focused password field directly: {active_element_summary(driver)}")

            active = driver.switch_to.active_element
            active.send_keys(Keys.CONTROL, "a")
            active.send_keys(Keys.BACKSPACE)
            active.send_keys(credentials.password)
            time.sleep(0.4)
            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"Typed password via keyboard: {login_form_value_lengths(driver)} | active={active_element_summary(driver)}",
                )

            value_lengths = login_form_value_lengths(driver)
            if "passwordLength=0" in value_lengths:
                raise RuntimeError(f"Password field did not accept typed input: {value_lengths}")

            _cdp_focus(driver, login_button_node_id)
            time.sleep(0.4)
            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"Focused submit button directly: {active_element_summary(driver)} | {login_form_value_lengths(driver)}",
                )

            _cdp_click(driver, login_button_node_id)
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, "Keyboard login submit clicked")
            return
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.WARNING, ctx, f"Keyboard login attempt failed: {last_error}")
            time.sleep(0.25)

    raise RuntimeError(f"Keyboard login fallback could not access the Reddit login form: {last_error}")


def ensure_reddit_login_page_ready(
    driver: webdriver.Chrome,
    timeout: int,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> tuple[WebElement, WebElement]:
    deadline = time.monotonic() + max(1, timeout)
    last_seen_url = ""

    while time.monotonic() < deadline:
        current_url = (driver.current_url or "").strip()
        lowered_url = current_url.lower()
        last_seen_url = current_url
        if "reddit.com/login" not in lowered_url:
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Waiting for reddit.com/login; current_url={lowered_url}")
            time.sleep(0.25)
            continue
        if not is_canonical_reddit_login_url(current_url):
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Login page URL is non-canonical, forcing clean /login/: {current_url}")
            force_canonical_reddit_login(driver, logger=logger, ctx=ctx)
            time.sleep(0.25)
            continue

        block_reason = detect_reddit_block(driver)
        if block_reason:
            raise RedditChallengeError(block_reason)

        try:
            username_field = find_first_interactable(driver, LOGIN_USERNAME_SELECTORS, 1)
            password_field = find_first_interactable(driver, LOGIN_PASSWORD_SELECTORS, 1)
            find_first_interactable(driver, LOGIN_BUTTON_SELECTORS, 1)
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, "Plain DOM login form is interactable")
            return username_field, password_field
        except Exception as plain_error:
            try:
                username_field = find_first_interactable_deep(driver, LOGIN_USERNAME_SELECTORS, 1)
                password_field = find_first_interactable_deep(driver, LOGIN_PASSWORD_SELECTORS, 1)
                find_first_interactable_deep(driver, LOGIN_BUTTON_SELECTORS, 1)
                if logger is not None and ctx is not None:
                    log_ctx(logger, logging.INFO, ctx, "Deep/shadow DOM login form is interactable")
                return username_field, password_field
            except Exception:
                last_seen_url = f"{current_url} | plain_dom_error={plain_error}"
                if logger is not None and ctx is not None:
                    log_ctx(logger, logging.INFO, ctx, f"Login form not ready yet: {page_diagnostics(driver)}")
                time.sleep(0.25)
                continue

        if "reddit.com/login" not in lowered_url:
            time.sleep(0.25)

    raise RuntimeError(
        f"Automation never reached a ready Reddit login form. Last URL seen: {last_seen_url or '<unknown>'}"
    )


def click_first_clickable(driver: webdriver.Chrome, selectors: tuple[str, ...], timeout: int) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            element = wait_clickable(driver, selector, timeout)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            element.click()
            return
        except Exception as error:  # noqa: BLE001
            last_error = error
            try:
                element = WebDriverWait(driver, 1).until(EC.presence_of_element_located(locator(selector)))
                driver.execute_script("arguments[0].click();", element)
                return
            except Exception:
                pass
    raise RuntimeError(f"Could not click any expected selector: {selectors!r}; last_error={last_error}")


def is_logged_in(driver: webdriver.Chrome, username: str) -> bool:
    current_url = driver.current_url.lower()

    selectors = (
        "css=span.user a[href*='/user/']",
        "css=a[href*='/logout']",
        "css=form.logout-button",
        f"xpath=//a[contains(@href, '/user/{username}')]",
    )
    if any(exists(driver, selector, timeout=1.0) for selector in selectors):
        return True

    text = body_text(driver).lower()
    if (
        "log out" in text
        or f"u/{username.lower()}" in text
        or "logged in as" in text
        or "create post" in text
    ):
        return True

    if "/login" in current_url:
        return False
    return False


def detect_reddit_login_error(driver: webdriver.Chrome) -> str | None:
    lowered_text = body_text(driver).lower()
    lowered_url = (driver.current_url or "").lower()

    if "something went wrong logging in" in lowered_text:
        return "Reddit said 'Something went wrong logging in. Please try again.'"
    if "server error. try again later." in lowered_text:
        return "Reddit said 'Server error. Try again later.'"
    if "we had a server error" in lowered_text:
        return "Reddit said it had a server error"
    if "temporarily unavailable" in lowered_text and "/login" in lowered_url:
        return "Reddit login appears temporarily unavailable"
    return None


def detect_reddit_block(driver: webdriver.Chrome) -> str | None:
    lowered_url = (driver.current_url or "").lower()
    lowered_text = body_text(driver).lower()

    if "checkpoint" in lowered_url or "verification" in lowered_url:
        return "Reddit checkpoint/verification page detected"
    if "two-factor" in lowered_text or "2-factor" in lowered_text or "authenticator" in lowered_text:
        return "2FA prompt detected"
    if "verification code" in lowered_text or "one-time code" in lowered_text:
        return "Verification-code prompt detected"
    if "captcha" in lowered_text or "are you human" in lowered_text:
        return "Captcha challenge detected"
    if "incorrect username or password" in lowered_text:
        return "Incorrect Reddit username or password"
    if "you are already logged in and will be redirected shortly" in lowered_text:
        return None
    return None


def is_retryable_account_error(error: Exception | str) -> bool:
    lowered = str(error).lower()
    retry_markers = (
        "old reddit prefs redirected back to login",
        "reddit login did not complete",
        "server error. try again later",
        "something went wrong logging in",
        "temporarily unavailable",
        "err_tunnel_connection_failed",
        "read timed out",
        "connection reset",
        "connection aborted",
        "unknown error: net::",
        "no such window: target window already closed",
        "web view not found",
        "could not verify an authenticated old reddit session",
        "automation never reached a ready reddit login form",
    )
    return any(marker in lowered for marker in retry_markers)


def login_to_reddit(
    driver: webdriver.Chrome,
    credentials: RedditCredentials,
    timeout: int,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    open_reddit_tab(driver, REDDIT_LOGIN_URL, logger=logger, ctx=ctx)
    force_canonical_reddit_login(driver, logger=logger, ctx=ctx)
    dom_login_error: Exception | None = None
    try:
        username_field, password_field = ensure_reddit_login_page_ready(
            driver,
            timeout,
            logger=logger,
            ctx=ctx,
        )
        set_input_element(driver, username_field, credentials.username, "reddit username")
        set_input_element(driver, password_field, credentials.password, "reddit password")
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, "DOM login inputs populated")
        click_first_clickable(driver, LOGIN_BUTTON_SELECTORS, timeout)
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, "DOM login submit clicked")
    except Exception as error:  # noqa: BLE001
        dom_login_error = error
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.WARNING, ctx, f"DOM login path failed, trying keyboard fallback: {error}")
        else:
            safe_print(f"[{credentials.username}] DOM login path failed, trying keyboard fallback: {error}")
        login_to_reddit_via_keyboard(driver, credentials, timeout, logger=logger, ctx=ctx)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        block_reason = detect_reddit_block(driver)
        if block_reason:
            raise RedditChallengeError(block_reason)
        login_error = detect_reddit_login_error(driver)
        if login_error:
            raise RedditTransientError(login_error)
        if is_logged_in(driver, credentials.username):
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Login confirmed: {page_diagnostics(driver)}")
            return
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, f"Waiting for logged-in state: {page_diagnostics(driver)}")
        time.sleep(0.75)

    if exists(driver, "css=input[name='username']", timeout=1.0) or exists(driver, "css=input[name='user']", timeout=1.0):
        block_reason = detect_reddit_block(driver)
        if block_reason:
            raise RedditChallengeError(block_reason)
        login_error = detect_reddit_login_error(driver)
        if login_error:
            raise RedditTransientError(login_error)
        if dom_login_error is not None:
            raise RuntimeError(f"Reddit login did not complete after DOM/keyboard attempts. DOM error: {dom_login_error}")
        raise RuntimeError("Reddit login did not complete")


def submitted_posts_url(username: str) -> str:
    return f"{OLD_REDDIT_BASE_URL}/user/{username}/submitted/"


def prefs_url() -> str:
    return f"{OLD_REDDIT_BASE_URL}/prefs/"


def ensure_authenticated_old_reddit_session(
    driver: webdriver.Chrome,
    username: str,
    timeout: int,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    open_reddit_tab(driver, prefs_url(), logger=logger, ctx=ctx)
    WebDriverWait(driver, timeout).until(
        lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
    )

    current_url = (driver.current_url or "").lower()
    if "/login" in current_url:
        raise RedditTransientError("Old Reddit prefs redirected back to login, so the session is not authenticated")

    if exists(driver, "css=form.logout-button", timeout=1.5) or exists(
        driver, "css=a[href*='/logout']", timeout=1.5
    ):
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, "Authenticated old Reddit session confirmed via logout control")
        return

    text = body_text(driver).lower()
    if "preferences" in text or "save options" in text or f"/user/{username.lower()}" in current_url:
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, "Authenticated old Reddit session confirmed via prefs page content")
        return

    raise RedditTransientError("Could not verify an authenticated old Reddit session")


def ensure_submitted_page(
    driver: webdriver.Chrome,
    username: str,
    timeout: int,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> None:
    open_reddit_tab(driver, submitted_posts_url(username), logger=logger, ctx=ctx)
    WebDriverWait(driver, timeout).until(lambda current_driver: current_driver.execute_script("return document.readyState") == "complete")

    block_reason = detect_reddit_block(driver)
    if block_reason:
        raise RedditChallengeError(block_reason)
    login_error = detect_reddit_login_error(driver)
    if login_error:
        raise RedditTransientError(login_error)

    current_url = (driver.current_url or "").lower()
    if "/login" in current_url:
        raise RedditTransientError("Reddit redirected back to the login page")
    if logger is not None and ctx is not None:
        log_ctx(logger, logging.INFO, ctx, f"Submitted page ready: {page_diagnostics(driver)}")


def thing_locator_from_key(key: str) -> str:
    if key.startswith("fullname:"):
        return f"css=div.thing[data-fullname='{key.removeprefix('fullname:')}']"
    if key.startswith("id:"):
        return f"css=div.thing#{key.removeprefix('id:')}"
    return ""


def post_identity(thing: WebElement, index: int) -> str:
    fullname = (thing.get_attribute("data-fullname") or "").strip()
    if fullname:
        return f"fullname:{fullname}"
    html_id = (thing.get_attribute("id") or "").strip()
    if html_id:
        return f"id:{html_id}"
    return f"index:{index}"


def page_delete_form_count(driver: webdriver.Chrome) -> int:
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, "form.del-button, form.toggle.del-button"))
    except WebDriverException:
        return 0


def click_delete_on_first_post(driver: webdriver.Chrome, timeout: int) -> bool:
    things = driver.find_elements(By.CSS_SELECTOR, "div.thing.link, div.thing")
    for index, thing in enumerate(things):
        try:
            delete_form = thing.find_element(By.CSS_SELECTOR, "form.del-button, form.toggle.del-button")
        except NoSuchElementException:
            continue

        toggle_candidates: list[WebElement] = []
        for selector in DELETE_BUTTON_SELECTORS:
            by, value = locator(selector)
            found = delete_form.find_elements(by, value)
            if found:
                toggle_candidates = found
                break
        if not toggle_candidates:
            continue

        identity = post_identity(thing, index)
        count_before = page_delete_form_count(driver)
        toggle_button = toggle_candidates[0]
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggle_button)
        try:
            toggle_button.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", toggle_button)

        yes_link = None
        yes_deadline = time.monotonic() + timeout
        while time.monotonic() < yes_deadline:
            for selector in DELETE_CONFIRM_SELECTORS:
                by, value = locator(selector)
                candidates = delete_form.find_elements(by, value)
                if candidates:
                    yes_link = candidates[0]
                    break
            if yes_link is not None:
                break
            time.sleep(0.25)

        if yes_link is None:
            continue

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", yes_link)
        try:
            yes_link.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", yes_link)

        locator_value = thing_locator_from_key(identity)
        try:
            WebDriverWait(driver, timeout).until(
                lambda current_driver: (
                    page_delete_form_count(current_driver) < count_before
                    or (locator_value and not exists(current_driver, locator_value, timeout=0.4))
                )
            )
        except TimeoutException:
            current_url = driver.current_url
            driver.get(current_url)
            WebDriverWait(driver, timeout).until(
                lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
            )
        return True
    return False


def get_next_page_url(driver: webdriver.Chrome) -> str | None:
    for selector in NEXT_PAGE_SELECTORS:
        by, value = locator(selector)
        candidates = driver.find_elements(by, value)
        for candidate in candidates:
            href = (candidate.get_attribute("href") or "").strip()
            if href:
                return href
    return None


def delete_all_posts(
    driver: webdriver.Chrome,
    username: str,
    config: RedditAdsPowerConfig,
    logger: logging.Logger | None = None,
    ctx: RedditRuntimeContext | None = None,
) -> int:
    deleted_count = 0
    visited_urls: set[str] = set()

    ensure_authenticated_old_reddit_session(
        driver,
        username,
        config.wait_seconds,
        logger=logger,
        ctx=ctx,
    )
    ensure_submitted_page(driver, username, config.wait_seconds, logger=logger, ctx=ctx)
    while True:
        current_url = driver.current_url
        visited_urls.add(current_url)
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, f"Delete loop page: {current_url}")

        block_reason = detect_reddit_block(driver)
        if block_reason:
            raise RedditChallengeError(block_reason)

        while click_delete_on_first_post(driver, config.delete_wait_seconds):
            deleted_count += 1
            if logger is not None and ctx is not None:
                log_ctx(logger, logging.INFO, ctx, f"Deleted post count is now {deleted_count}")
            block_reason = detect_reddit_block(driver)
            if block_reason:
                raise RedditChallengeError(block_reason)

        next_page_url = get_next_page_url(driver)
        if not next_page_url or next_page_url in visited_urls:
            if logger is not None and ctx is not None:
                log_ctx(
                    logger,
                    logging.INFO,
                    ctx,
                    f"No further submitted pages found; final deleted count {deleted_count}",
                )
            break
        if logger is not None and ctx is not None:
            log_ctx(logger, logging.INFO, ctx, f"Moving to next submitted page: {next_page_url}")
        driver.get(next_page_url)
        WebDriverWait(driver, config.wait_seconds).until(
            lambda current_driver: current_driver.execute_script("return document.readyState") == "complete"
        )

    return deleted_count


def capture_failure_screenshot(
    driver: webdriver.Chrome | None,
    credentials: RedditCredentials,
    profile_id: str | None,
    reason: str,
) -> Path | None:
    if driver is None:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = FAILURE_SCREENSHOT_DIR / (
        f"{timestamp}_{safe_filename(credentials.username)}_{safe_filename(profile_id or 'no_profile')}_{safe_filename(reason)}.png"
    )

    try:
        FAILURE_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(screenshot_path))
    except Exception:
        return None

    return screenshot_path


def cleanup(client: AdsPowerClient, profile_id: str, driver: webdriver.Chrome | None) -> None:
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass

    for attempt in range(2):
        try:
            run_adspower_call("stop_browser", client.stop_browser, profile_id, retries=3)
            break
        except Exception:
            if attempt == 0:
                time.sleep(2)

    time.sleep(1.0)
    for attempt in range(3):
        try:
            run_adspower_call("delete_profiles", client.delete_profiles, [profile_id], retries=3)
            return
        except Exception as error:
            if attempt < 2:
                time.sleep(2)
            else:
                safe_print(f"[cleanup:{profile_id}] AdsPower delete failed: {error}")


def run_account(credentials: RedditCredentials, config: RedditAdsPowerConfig) -> AccountResult:
    client = AdsPowerClient(config)
    total_deleted_count = 0
    max_attempts = max(1, REDDIT_ACCOUNT_RETRY_ATTEMPTS)
    last_error: Exception | None = None
    last_screenshot_path: str | None = None
    last_profile_id: str | None = None

    for attempt in range(1, max_attempts + 1):
        ctx = RedditRuntimeContext(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f"),
            user_id=credentials.username,
        )
        logger = setup_logger(ctx)
        profile_id: str | None = None
        driver: webdriver.Chrome | None = None
        deleted_count = 0

        try:
            log_ctx(logger, logging.INFO, ctx, f"Starting attempt {attempt}/{max_attempts}")
            log_ctx(logger, logging.INFO, ctx, "Creating AdsPower profile with a random saved proxy...")
            profile_id = create_profile_with_random_saved_proxy(client, credentials, config)
            log_ctx(logger, logging.INFO, ctx, f"Created profile {profile_id}.")

            browser = run_adspower_call("start_browser", client.start_browser, profile_id)
            log_ctx(logger, logging.INFO, ctx, f"Browser started. Debugger address: {browser.debugger_address}")
            driver = attach_driver(browser)
            log_ctx(logger, logging.INFO, ctx, f"Selenium attached: {page_diagnostics(driver)}")

            log_ctx(logger, logging.INFO, ctx, "Logging into Reddit...")
            login_to_reddit(driver, credentials, config.wait_seconds, logger=logger, ctx=ctx)

            log_ctx(logger, logging.INFO, ctx, "Deleting submitted posts on old Reddit...")
            deleted_count = delete_all_posts(
                driver,
                credentials.username,
                config,
                logger=logger,
                ctx=ctx,
            )
            total_deleted_count += deleted_count
            log_ctx(logger, logging.INFO, ctx, f"Finished. Deleted {deleted_count} post(s) this attempt.")

            return AccountResult(
                username=credentials.username,
                status="success",
                posts_deleted=total_deleted_count,
                profile_id=profile_id,
                original_input=f"{credentials.username} {credentials.password}",
                finished_at_utc=utc_now_iso(),
            )
        except Exception as error:  # noqa: BLE001
            total_deleted_count += deleted_count
            last_error = error
            last_profile_id = profile_id
            screenshot_path = capture_failure_screenshot(
                driver,
                credentials,
                profile_id,
                type(error).__name__,
            )
            last_screenshot_path = str(screenshot_path) if screenshot_path else None
            retryable = not isinstance(error, RedditChallengeError) and is_retryable_account_error(error)
            log_ctx(
                logger,
                logging.ERROR,
                ctx,
                f"Failed on attempt {attempt}/{max_attempts}: {error} | retryable={retryable}",
            )
            if not retryable or attempt >= max_attempts:
                return AccountResult(
                    username=credentials.username,
                    status="failed",
                    posts_deleted=total_deleted_count,
                    profile_id=profile_id,
                    original_input=f"{credentials.username} {credentials.password}",
                    error=str(error),
                    screenshot_path=last_screenshot_path,
                    finished_at_utc=utc_now_iso(),
                )
            backoff_seconds = REDDIT_RETRY_BACKOFF_SECONDS * attempt
            log_ctx(
                logger,
                logging.INFO,
                ctx,
                (
                    f"Retrying account with a fresh profile/proxy in {backoff_seconds:.1f}s "
                    f"because the failure looks transient."
                ),
            )
            time.sleep(backoff_seconds)
        finally:
            if profile_id:
                log_ctx(logger, logging.INFO, ctx, "Cleaning up temporary AdsPower profile...")
                cleanup(client, profile_id, driver)

    return AccountResult(
        username=credentials.username,
        status="failed",
        posts_deleted=total_deleted_count,
        profile_id=last_profile_id,
        original_input=f"{credentials.username} {credentials.password}",
        error=str(last_error) if last_error else "Account processing failed",
        screenshot_path=last_screenshot_path,
        finished_at_utc=utc_now_iso(),
    )


def write_run_report(results: list[AccountResult], concurrency: int) -> Path:
    RUN_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = RUN_REPORT_DIR / f"reddit_cleanup_run_{timestamp}.json"
    payload = {
        "generated_at_utc": utc_now_iso(),
        "concurrency": concurrency,
        "total_accounts": len(results),
        "successful_accounts": sum(1 for result in results if result.status == "success"),
        "failed_accounts": sum(1 for result in results if result.status == "failed"),
        "total_deleted_posts": sum(result.posts_deleted for result in results),
        "results": [asdict(result) for result in results],
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


def run_cleanup(credentials_list: list[RedditCredentials], concurrency: int, config: RedditAdsPowerConfig) -> list[AccountResult]:
    results: list[AccountResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_map = {
            executor.submit(run_account, credentials, config): credentials.username
            for credentials in credentials_list
        }
        for future in as_completed(future_map):
            username = future_map[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001
                safe_print(f"[{username}] Unexpected worker failure: {error}")
                matching_credentials = next(
                    (credentials for credentials in credentials_list if credentials.username == username),
                    None,
                )
                result = AccountResult(
                    username=username,
                    status="failed",
                    posts_deleted=0,
                    profile_id=None,
                    original_input=(
                        f"{matching_credentials.username} {matching_credentials.password}"
                        if matching_credentials is not None
                        else None
                    ),
                    error=f"Unexpected worker failure: {error}",
                    finished_at_utc=utc_now_iso(),
                )
            results.append(result)
    results.sort(key=lambda item: item.username.lower())
    return results


def run_self_test() -> int:
    valid = parse_credentials("example_user secret_password")
    assert valid.username == "example_user"
    assert valid.password == "secret_password"

    invalid_values = ["", "example_user", ":password", "username:", "user pass extra"]
    for raw_value in invalid_values:
        try:
            parse_credentials(raw_value)
        except ValueError:
            continue
        raise AssertionError(f"Expected invalid credentials to fail: {raw_value!r}")

    print("Self-test passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create temporary AdsPower Reddit browsers and delete submitted posts."
    )
    parser.add_argument("--self-test", action="store_true", help="run parser dry tests and exit")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()

    safe_print(f"Script path: {Path(__file__).resolve()}")
    safe_print(f"Script modified UTC: {script_timestamp_label()}")

    base_config = RedditAdsPowerConfig()
    concurrency = prompt_concurrency()
    extension_category_id, extension_category_name = prompt_extension_category_override()
    disable_extensions_on_launch = prompt_disable_extensions_on_launch()
    credentials_list = prompt_credentials()
    if not credentials_list:
        safe_print("No credentials provided.")
        return 1

    config = RedditAdsPowerConfig(
        api_base=base_config.api_base,
        api_key=base_config.api_key,
        api_timeout_seconds=base_config.api_timeout_seconds,
        api_retries=base_config.api_retries,
        group_id=base_config.group_id,
        wait_seconds=base_config.wait_seconds,
        delete_wait_seconds=base_config.delete_wait_seconds,
        extension_category_id=extension_category_id,
        extension_category_name=extension_category_name,
        disable_extensions_on_launch=disable_extensions_on_launch,
    )

    if extension_category_id or extension_category_name:
        client = AdsPowerClient(config)
        try:
            config = resolve_extension_category_override(client, config)
            safe_print(
                f"Using AdsPower extension category ID {config.extension_category_id} for temp profiles."
            )
            if config.extension_category_name:
                safe_print(f"Requested extension category name: {config.extension_category_name}")
        except Exception as error:  # noqa: BLE001
            safe_print(f"Could not resolve the requested AdsPower extension category: {error}")
            return 1
    if config.disable_extensions_on_launch:
        safe_print("Browser launch args will include --disable-extensions.")

    safe_print(f"Starting Reddit cleanup for {len(credentials_list)} account(s) with concurrency {concurrency}.")
    results = run_cleanup(credentials_list, concurrency, config)
    report_path = write_run_report(results, concurrency)

    safe_print("")
    safe_print("Run summary:")
    for result in results:
        message = (
            f"{result.username} | {result.status} | deleted={result.posts_deleted}"
            + (f" | error={result.error}" if result.error else "")
            + (f" | screenshot={result.screenshot_path}" if result.screenshot_path else "")
        )
        safe_print(message)
    failed_results = [result for result in results if result.status == "failed" and result.original_input]
    if failed_results:
        safe_print("")
        safe_print("Failed accounts to retry:")
        for result in failed_results:
            safe_print(result.original_input or "")
    safe_print(f"Saved JSON report: {report_path}")
    return 0 if all(result.status == "success" for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
