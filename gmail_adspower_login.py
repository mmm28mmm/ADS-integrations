from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
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

from thundr_bot.adspower_client import AdsPowerCapabilityError, AdsPowerClient  # noqa: E402


# Edit these three values once. The proxy port is intentionally prompted at launch.
PROXY_HOST = "gw.dataimpulse.com"
PROXY_USERNAME = "51479b009fdfae8791ba__cr.de"
PROXY_PASSWORD = "85542610de69aff0"

ADSPOWER_API_BASE = os.getenv("ADSPOWER_API_BASE", "http://local.adspower.net:50325")
ADSPOWER_API_KEY = os.getenv("ADSPOWER_API_KEY") or None
ADSPOWER_GROUP_ID = "7037614"

GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_LOGIN_URL = (
    "https://accounts.google.com/v3/signin/identifier"
    f"?service=mail&continue={quote(GMAIL_INBOX_URL, safe='')}"
    "&hl=en&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
)
DEFAULT_WAIT_SECONDS = 18
BROWSER_WINDOW_WIDTH = 1450
BROWSER_WINDOW_HEIGHT = 950
POST_PASSWORD_WAIT_SECONDS = 2.0
PORT_HISTORY_PATH = ROOT_DIR / "logs" / "gmail_used_ports.log"
FAILURE_SCREENSHOT_DIR = ROOT_DIR / "logs" / "gmail_failure_screenshots"


@dataclass(frozen=True)
class GmailCredentials:
    email: str
    password: str
    recovery_email: str


@dataclass(frozen=True)
class GmailAdsPowerConfig:
    api_base: str = ADSPOWER_API_BASE
    api_key: str | None = ADSPOWER_API_KEY
    api_timeout_seconds: int = 20
    api_retries: int = 3
    group_id: str = ADSPOWER_GROUP_ID
    wait_seconds: int = DEFAULT_WAIT_SECONDS


class TwoFactorPromptError(RuntimeError):
    """Raised when Google asks for a code-based 2FA prompt this helper should not handle."""


def mask_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[:2] + "***" + local[-1:]
    return f"{masked_local}@{domain}"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "unknown"


def parse_credentials(raw_value: str) -> GmailCredentials:
    raw_value = raw_value.strip()
    if ":" not in raw_value:
        raise ValueError("Credentials must use email:password:recovery-email format")

    email, remainder = raw_value.split(":", 1)
    if ":" not in remainder:
        raise ValueError("Credentials must use email:password:recovery-email format")
    password, recovery_email = remainder.rsplit(":", 1)

    email, password, recovery_email = [part.strip() for part in (email, password, recovery_email)]
    if not email or "@" not in email:
        raise ValueError("The email part is missing or invalid")
    if not password:
        raise ValueError("The password part is missing")
    if not recovery_email or "@" not in recovery_email:
        raise ValueError("The recovery-email part is missing or invalid")

    return GmailCredentials(email=email, password=password, recovery_email=recovery_email)


def prompt_credentials() -> GmailCredentials:
    while True:
        raw_value = input("Paste Gmail credentials (email:password:recovery-email): ").strip()
        try:
            return parse_credentials(raw_value)
        except ValueError as error:
            print(f"Invalid input: {error}")


def read_port_history() -> list[str]:
    if not PORT_HISTORY_PATH.exists():
        return []
    ports: list[str] = []
    try:
        for raw_line in PORT_HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            parts = raw_line.strip().split()
            if len(parts) >= 2 and re.fullmatch(r"\d{1,5}", parts[-1]):
                ports.append(parts[-1])
    except OSError:
        return []
    return ports


def record_proxy_port(port: str) -> None:
    try:
        PORT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with PORT_HISTORY_PATH.open("a", encoding="utf-8") as history_file:
            history_file.write(f"{timestamp} {port}\n")
    except OSError as error:
        print(f"Warning: could not record proxy port history: {error}")


def prompt_proxy_port() -> str:
    port_history = read_port_history()
    last_port = port_history[-1] if port_history else None
    prompt_suffix = f" (previous used port: {last_port})" if last_port else ""
    while True:
        value = input(f"Proxy port{prompt_suffix}: ").strip()
        if re.fullmatch(r"\d{2,5}", value):
            port = int(value)
            if 1 <= port <= 65535:
                if value in port_history:
                    print(f"Warning: port {value} appears in previous Gmail helper history.")
                return value
        print("Invalid proxy port. Enter a number from 1 to 65535.")


def validate_proxy_constants() -> None:
    placeholders = {
        "PROXY_HOST": PROXY_HOST,
        "PROXY_USERNAME": PROXY_USERNAME,
        "PROXY_PASSWORD": PROXY_PASSWORD,
    }
    missing = [name for name, value in placeholders.items() if value.startswith("EDIT_")]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Edit {names} at the top of gmail_adspower_login.py before running.")


def build_proxy_config(port: str) -> dict[str, str]:
    return {
        "proxy_soft": "other",
        "proxy_type": "http",
        "proxy_host": PROXY_HOST,
        "proxy_port": port,
        "proxy_user": PROXY_USERNAME,
        "proxy_password": PROXY_PASSWORD,
    }


def build_profile_payload(credentials: GmailCredentials, port: str, config: GmailAdsPowerConfig) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_email = re.sub(r"[^A-Za-z0-9_.-]+", "_", credentials.email)
    return {
        "name": f"gmail_{safe_email}_{timestamp}",
        "group_id": config.group_id,
        "remark": f"GMAIL LOGIN HELPER {timestamp}",
        "platform": "gmail.com",
        "username": credentials.email,
        "password": credentials.password,
        "user_proxy_config": build_proxy_config(port),
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


def create_profile_with_proxy(
    client: AdsPowerClient,
    credentials: GmailCredentials,
    port: str,
    config: GmailAdsPowerConfig,
) -> str:
    payload = build_profile_payload(credentials, port, config)
    try:
        return client.create_profile(payload)
    except RuntimeError as first_error:
        fallback_payload = dict(payload)
        fallback_payload.pop("user_proxy_config", None)
        try:
            profile_id = client.create_profile(fallback_payload)
        except Exception as fallback_error:  # noqa: BLE001
            if "proxy" in str(first_error).lower() or "proxy" in str(fallback_error).lower():
                raise RuntimeError(
                    "AdsPower rejected profile creation because of proxy settings. "
                    "This usually happens when the logged-in AdsPower account cannot create profiles "
                    "with custom proxy configs, or the account/group requires a saved proxy_id. "
                    f"Initial create error: {first_error}; fallback-without-inline-proxy error: {fallback_error}"
                ) from fallback_error
            raise
        try:
            proxy_value = f"{PROXY_HOST}:{port}:{PROXY_USERNAME}:{PROXY_PASSWORD}"
            client.update_profile_http_proxy(profile_id, proxy_value)
            return profile_id
        except Exception as proxy_error:  # noqa: BLE001
            try:
                client.delete_profiles([profile_id])
            except Exception:
                pass
            raise RuntimeError(
                "Created profile after inline proxy fallback, but failed to apply proxy. "
                f"Initial create error: {first_error}; proxy update error: {proxy_error}"
            ) from proxy_error


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


def set_input(driver: webdriver.Chrome, selector: str, value: str, timeout: int) -> None:
    field = wait_visible(driver, selector, timeout)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", field)

    # Avoid coordinate-based clicks here. RDP/DPI changes and Google transition
    # overlays can intercept real clicks even when the input is present.
    # Prefer real key events over JS value injection because Google login can
    # treat synthetic-only input as suspicious.
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

    # Last fallback for stubborn fields only.
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
    if current_value == value:
        return
    raise RuntimeError(f"Could not set input value for selector {selector!r}")


def focus_current_tab(driver: webdriver.Chrome) -> None:
    try:
        driver.execute_script("window.focus();")
    except WebDriverException:
        pass


def focus_url_tab(driver: webdriver.Chrome, url_fragment: str) -> None:
    lowered_fragment = url_fragment.lower()
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if lowered_fragment in driver.current_url.lower():
                focus_current_tab(driver)
                return
        except WebDriverException:
            continue
    focus_current_tab(driver)


def bring_browser_window_to_front(title_hints: tuple[str, ...] = ()) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    titles: list[tuple[int, str]] = []
    default_hints = (
        "gmail",
        "google",
        "adspower",
        "chrome",
        "inbox",
        "willkommen",
        "anmeldung",
        "verify",
        "bestätigen",
    )
    lowered_hints = tuple(hint.lower() for hint in (*default_hints, *title_hints) if hint)

    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        lowered = title.lower()
        if any(hint in lowered for hint in lowered_hints):
            titles.append((hwnd, title))
        return True

    try:
        user32.EnumWindows(enum_windows_proc(_callback), 0)
        if not titles:
            return False
        hwnd = titles[0][0]
        user32.AllowSetForegroundWindow(-1)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
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


def click_next(driver: webdriver.Chrome, timeout: int, preferred_step: str | None = None) -> None:
    email_selectors = (
        "css=#identifierNext",
        "xpath=//*[@id='identifierNext']",
    )
    password_selectors = (
        "css=#passwordNext",
        "xpath=//*[@id='passwordNext']",
    )
    generic_selectors = (
        "xpath=//*[@id='identifierNext' or @id='passwordNext']",
        "xpath=//button[.//span[normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste'] or normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste']",
        "xpath=//*[@role='button'][.//span[normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste'] or normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste']",
    )
    recovery_selectors = (
        "xpath=//button[not(@disabled) and (.//span[normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste'] or normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste')]",
        "xpath=//*[@role='button' and not(@aria-disabled='true')][.//span[normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste'] or normalize-space()='Next' or normalize-space()='Weiter' or normalize-space()='Nächste']",
    )
    if preferred_step == "password":
        selectors = password_selectors + email_selectors + generic_selectors
    elif preferred_step == "recovery":
        selectors = recovery_selectors + generic_selectors
    elif preferred_step == "email":
        selectors = email_selectors + password_selectors + generic_selectors
    else:
        selectors = email_selectors + password_selectors + generic_selectors

    last_error: Exception | None = None
    for selector in selectors:
        try:
            element = wait_clickable(driver, selector, max(1, timeout))
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
    raise RuntimeError(f"Could not click Google's Next button: {last_error}")


def click_by_text_fragments(driver: webdriver.Chrome, fragments: tuple[str, ...], timeout: int) -> bool:
    lowered_fragments = tuple(fragment.lower() for fragment in fragments)
    deadline = time.monotonic() + max(1, timeout)
    script = """
        const fragments = arguments[0].map((value) => String(value).toLowerCase());
        const nodes = Array.from(document.querySelectorAll('div, span, button, [role="button"], [role="link"]'));
        const matches = [];
        for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            if (!text || !fragments.every((fragment) => text.includes(fragment))) {
                continue;
            }
            matches.push({node, textLength: text.length});
        }
        matches.sort((left, right) => left.textLength - right.textLength);
        for (const match of matches) {
            const node = match.node;
            const clickable = node.closest('[role="button"], [role="link"], button, a') || node;
            clickable.scrollIntoView({block: 'center'});
            clickable.click();
            return true;
        }
        return false;
    """
    while time.monotonic() < deadline:
        try:
            if driver.execute_script(script, list(lowered_fragments)):
                return True
        except WebDriverException:
            pass
        time.sleep(0.5)
    return False


def body_text(driver: webdriver.Chrome) -> str:
    try:
        return " ".join((driver.find_element(By.TAG_NAME, "body").text or "").split())
    except WebDriverException:
        return ""


def maybe_fill_recovery_email(
    driver: webdriver.Chrome,
    credentials: GmailCredentials,
    timeout: int,
) -> bool:
    recovery_input_selectors = (
        "css=input[name='knowledgePreregisteredEmailResponse']",
        "css=input[type='email']",
        "xpath=//input[@type='email' or @type='text' or contains(@name, 'Email') or contains(@aria-label, 'email') or contains(@aria-label, 'E-Mail')]",
    )
    for selector in recovery_input_selectors:
        if not exists(driver, selector, timeout=4):
            continue
        set_input(driver, selector, credentials.recovery_email, timeout)
        click_next(driver, max(5, int(timeout * 0.4)), preferred_step="recovery")
        return True
    return False


def maybe_choose_recovery_email_method(driver: webdriver.Chrome, timeout: int) -> bool:
    choices = (
        ("confirm", "recovery", "email"),
        ("bestätigen", "wiederherstellungs", "mail"),
        ("wiederherstellungs", "e-mail"),
        ("e-mail-adresse", "kontowiederherstellung", "bestätigen"),
        ("kontowiederherstellung", "bestätigen"),
    )
    for fragments in choices:
        if click_by_text_fragments(driver, fragments, timeout):
            return True
    return False


def maybe_handle_add_recovery_info_page(
    driver: webdriver.Chrome,
    timeout: int,
) -> bool:
    text = body_text(driver).lower()
    url = driver.current_url.lower()
    looks_like_recovery_options = (
        "recoveryoptions" in url
        or "make sure that you can always sign in" in text
        or "add a recovery phone" in text
        or "enter recovery email" in text
        or "wiederherstell" in text
        or "telefonnummer zur kontowiederherstellung" in text
    )
    if not looks_like_recovery_options:
        return False

    cancel_clicked = click_by_text_fragments(driver, ("cancel",), timeout=4)
    if not cancel_clicked:
        cancel_clicked = click_by_text_fragments(driver, ("stornieren",), timeout=4)
    if not cancel_clicked:
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except WebDriverException:
            pass

    time.sleep(2)
    return True


def maybe_handle_home_address_page(driver: webdriver.Chrome, timeout: int) -> bool:
    text = body_text(driver).lower()
    url = driver.current_url.lower()
    looks_like_home_address = (
        "homeaddress" in url
        or "set a home address" in text
        or "home address" in text
        or "wohnadresse" in text
        or "heimatadresse" in text
    )
    if not looks_like_home_address:
        return False

    skipped = click_by_text_fragments(driver, ("skip",), timeout=4)
    if not skipped:
        skipped = click_by_text_fragments(driver, ("überspringen",), timeout=4)
    if not skipped:
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except WebDriverException:
            pass

    time.sleep(2)
    return True


def is_two_factor_or_code_prompt(driver: webdriver.Chrome) -> bool:
    if "mail.google.com/mail" in driver.current_url.lower():
        return False

    text = body_text(driver).lower()
    recovery_choice_seen = (
        "confirm your recovery email" in text
        or "recovery email address" in text
        or "wiederherstellungs-e-mail" in text
        or "e-mail-adresse zur kontowiederherstellung" in text
        or "kontowiederherstellung bestätigen" in text
    )
    if recovery_choice_seen:
        return False

    code_input_seen = (
        "code eingeben" in text
        or "code eingegeben" in text
        or "enter code" in text
        or "verification code" in text
    )
    authenticator_seen = (
        "google authenticator" in text
        or "authenticator" in text
        or "2-faktor-authentifizierung" in text
        or "2-factor authentication" in text
        or "2-step verification" in text
        or "bestätigungscode über die app" in text
    )
    return authenticator_seen or (code_input_seen and "recovery email" not in text and "wiederherstellungs-e-mail" not in text)


def is_gmail_inbox_ready(driver: webdriver.Chrome) -> bool:
    try:
        if "mail.google.com/mail" not in driver.current_url.lower():
            return False
    except WebDriverException:
        return False

    ready_selectors = (
        "css=div[role='main']",
        "css=div[gh='tl']",
        "css=table[role='grid']",
        "css=div[role='navigation']",
        "xpath=//*[contains(@aria-label, 'Search mail') or contains(@aria-label, 'E-Mail suchen')]",
        "xpath=//*[contains(normalize-space(), 'Inbox') or contains(normalize-space(), 'Posteingang')]",
    )
    return any(exists(driver, selector, timeout=0.4) for selector in ready_selectors)


def open_inbox_tab(driver: webdriver.Chrome, timeout: int) -> bool:
    try:
        driver.switch_to.new_window("tab")
    except WebDriverException:
        driver.execute_script("window.open('about:blank', '_blank');")
        driver.switch_to.window(driver.window_handles[-1])

    # Gmail often keeps loading background resources forever. Use async navigation
    # and wait for usable inbox DOM instead of blocking on full page load.
    driver.execute_script("window.location.assign(arguments[0]);", GMAIL_INBOX_URL)
    focus_url_tab(driver, "mail.google.com")
    normalize_browser_window(driver)
    bring_browser_window_to_front(("inbox", "gmail"))
    try:
        WebDriverWait(driver, timeout).until(is_gmail_inbox_ready)
    except TimeoutException:
        return is_gmail_inbox_ready(driver)
    return True


def handle_post_password_flow(
    driver: webdriver.Chrome,
    credentials: GmailCredentials,
    timeout: int,
) -> bool:
    deadline = time.monotonic() + max(8, timeout)
    while time.monotonic() < deadline:
        if "mail.google.com/mail" in driver.current_url.lower():
            return True
        if is_two_factor_or_code_prompt(driver):
            raise TwoFactorPromptError("2FA/code verification prompt detected")
        if maybe_handle_add_recovery_info_page(driver, timeout):
            maybe_handle_home_address_page(driver, timeout)
            return open_inbox_tab(driver, timeout)
        if maybe_handle_home_address_page(driver, timeout):
            return open_inbox_tab(driver, timeout)
        if maybe_choose_recovery_email_method(driver, timeout=2):
            time.sleep(1.5)
            maybe_fill_recovery_email(driver, credentials, timeout)
            time.sleep(2)
            if maybe_handle_add_recovery_info_page(driver, timeout):
                maybe_handle_home_address_page(driver, timeout)
            else:
                maybe_handle_home_address_page(driver, timeout)
            return open_inbox_tab(driver, timeout)
        time.sleep(0.75)
    return False


def classify_manual_state(driver: webdriver.Chrome) -> str:
    url = driver.current_url.lower()
    text = body_text(driver).lower()

    if is_gmail_inbox_ready(driver) or ("mail.google.com/mail" in url and "inbox" in url):
        return "Gmail inbox appears to be open."
    if "captcha" in text or "verify it" in text or "2-step verification" in text:
        return "Google is asking for manual verification."
    if "get a verification code" in text or "enter the code" in text:
        return "Google is asking for a verification code."
    if "couldn" in text and "sign you in" in text:
        return "Google sign-in did not complete; worker review is needed."
    return "Automation reached a manual checkpoint."


def navigate_to_gmail_login(driver: webdriver.Chrome, timeout: int) -> None:
    driver.get(GMAIL_LOGIN_URL)
    focus_url_tab(driver, "google.com")
    normalize_browser_window(driver)
    bring_browser_window_to_front(("gmail", "google"))

    try:
        WebDriverWait(driver, min(timeout, 10)).until(
            EC.presence_of_element_located(locator("css=input[type='email']"))
        )
        return
    except TimeoutException:
        pass

    # If Google still lands on a localized marketing page, avoid language-specific
    # sign-in buttons by forcing the account-login URL again from inside the tab.
    driver.execute_script("window.location.assign(arguments[0]);", GMAIL_LOGIN_URL)
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(locator("css=input[type='email']"))
    )


def run_gmail_login(driver: webdriver.Chrome, credentials: GmailCredentials, config: GmailAdsPowerConfig) -> str:
    wait_seconds = config.wait_seconds
    navigate_to_gmail_login(driver, wait_seconds)
    bring_browser_window_to_front(("gmail", "google", credentials.email.split("@", 1)[0]))

    set_input(driver, "css=input[type='email']", credentials.email, wait_seconds)
    click_next(driver, wait_seconds, preferred_step="email")

    set_input(driver, "css=input[type='password']", credentials.password, wait_seconds)
    password_submit_wait = max(6, int(wait_seconds * 0.7))
    click_next(driver, password_submit_wait, preferred_step="password")

    time.sleep(POST_PASSWORD_WAIT_SECONDS)
    if is_two_factor_or_code_prompt(driver):
        raise TwoFactorPromptError("2FA/code verification prompt detected")
    if not handle_post_password_flow(driver, credentials, wait_seconds):
        if is_two_factor_or_code_prompt(driver):
            raise TwoFactorPromptError("2FA/code verification prompt detected")
        open_inbox_tab(driver, wait_seconds)
        time.sleep(1)
        if is_two_factor_or_code_prompt(driver):
            raise TwoFactorPromptError("2FA/code verification prompt detected")
    time.sleep(2)
    if is_two_factor_or_code_prompt(driver):
        raise TwoFactorPromptError("2FA/code verification prompt detected")
    return classify_manual_state(driver)


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
    bring_browser_window_to_front()
    return driver


def cleanup(client: AdsPowerClient, profile_id: str, driver: webdriver.Chrome | None) -> None:
    if driver is not None:
        try:
            driver.quit()
        except Exception as error:  # noqa: BLE001
            print(f"Selenium quit warning: {error}")

    for attempt in range(1, 3):
        try:
            client.stop_browser(profile_id)
            break
        except Exception as error:  # noqa: BLE001
            if attempt == 2:
                print(f"AdsPower stop warning: {error}")
            else:
                time.sleep(2)

    for attempt in range(1, 3):
        try:
            client.delete_profiles([profile_id])
            print(f"Deleted AdsPower profile {profile_id}.")
            return
        except Exception as error:  # noqa: BLE001
            if attempt == 2:
                print(f"AdsPower delete failed for {profile_id}: {error}")
                return
            time.sleep(2)


def capture_failure_screenshot(
    driver: webdriver.Chrome | None,
    credentials: GmailCredentials,
    profile_id: str | None,
    reason: str,
) -> Path | None:
    if driver is None:
        print("Warning: could not capture failure screenshot because Selenium is not attached.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    profile_part = safe_filename(profile_id or "no_profile")
    email_part = safe_filename(mask_email(credentials.email).replace("***", "masked"))
    reason_part = safe_filename(reason)
    screenshot_path = FAILURE_SCREENSHOT_DIR / f"{timestamp}_{email_part}_{profile_part}_{reason_part}.png"

    try:
        FAILURE_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(screenshot_path))
    except Exception as error:  # noqa: BLE001
        print(f"Warning: could not capture failure screenshot: {error}")
        return None

    print(f"Saved failure screenshot: {screenshot_path}")
    return screenshot_path


def command_loop(client: AdsPowerClient, profile_id: str, driver: webdriver.Chrome | None) -> None:
    print("Commands: status, cleanup, done, help")
    while True:
        command = input("gmail-helper> ").strip().lower()
        if command in {"cleanup", "done", "exit", "quit"}:
            cleanup(client, profile_id, driver)
            return
        if command == "status":
            try:
                active = client.is_browser_active(profile_id)
            except AdsPowerCapabilityError:
                active = "unknown (AdsPower active endpoint unsupported)"
            except Exception as error:  # noqa: BLE001
                active = f"unknown ({error})"
            print(f"Profile: {profile_id} | Browser active: {active}")
            continue
        if command == "help":
            print("status = check browser state; cleanup/done = stop browser, delete profile, exit")
            continue
        if not command:
            continue
        print("Unknown command. Type help for available commands.")


def run_self_test() -> int:
    valid = parse_credentials("user@gmail.com:secret:recovery@example.com")
    assert valid.email == "user@gmail.com"
    assert valid.password == "secret"
    assert valid.recovery_email == "recovery@example.com"

    invalid_values = [
        "",
        "user@gmail.com:secret",
        "not-email:secret:recovery@example.com",
        "user@gmail.com::recovery@example.com",
        "user@gmail.com:secret:not-email",
    ]
    for raw_value in invalid_values:
        try:
            parse_credentials(raw_value)
        except ValueError:
            continue
        raise AssertionError(f"Expected invalid credentials to fail: {raw_value!r}")

    print("Self-test passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a temporary AdsPower Gmail login browser.")
    parser.add_argument("--self-test", action="store_true", help="run parser dry tests and exit")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()

    try:
        validate_proxy_constants()
    except RuntimeError as error:
        print(error)
        return 1

    config = GmailAdsPowerConfig()
    credentials = prompt_credentials()
    proxy_port = prompt_proxy_port()
    record_proxy_port(proxy_port)
    client = AdsPowerClient(config)

    profile_id: str | None = None
    driver: webdriver.Chrome | None = None

    try:
        print(f"Creating AdsPower profile for {mask_email(credentials.email)}...")
        profile_id = create_profile_with_proxy(client, credentials, proxy_port, config)
        print(f"Created AdsPower profile: {profile_id}")

        browser = client.start_browser(profile_id)
        print("Browser started. Attaching Selenium...")
        driver = attach_driver(browser)

        result = run_gmail_login(driver, credentials, config)
        print(result)
        print("Worker can now finish any verification manually in the opened browser.")
    except TwoFactorPromptError:
        print(f"{credentials.email} failed: 2FA/code verification prompt detected.")
        capture_failure_screenshot(driver, credentials, profile_id, "2fa_code_verification")
        if profile_id:
            cleanup(client, profile_id, driver)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"Automation stopped before completion: {error}")
        if profile_id:
            print("Profile is still available for manual review or cleanup.")
        else:
            return 1

    if profile_id is None:
        return 1

    command_loop(client, profile_id, driver)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
