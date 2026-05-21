from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SessionStatus = Literal["success", "failed", "max_retries", "stopped", "banned", "parked"]


def _load_dotenv_if_present(dotenv_path: str = ".env") -> None:
    candidates = [
        Path(dotenv_path),
        Path.cwd() / dotenv_path,
        Path(__file__).resolve().parents[1] / dotenv_path,
    ]
    dotenv_file = next((path for path in candidates if path.exists()), None)
    if dotenv_file is None:
        return

    with dotenv_file.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            # Trim optional wrapping quotes.
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            ):
                value = value[1:-1]

            # Keep already-exported shell values as higher priority.
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class BotConfig:
    api_base: str = "http://local.adspower.net:50325"
    api_key: str | None = None
    target_url: str = "https://thundr.com/chat?mode=text"

    element_wait_seconds: int = 12
    api_timeout_seconds: int = 15
    api_retries: int = 3

    max_session_restarts: int = 3
    max_consecutive_loop_errors: int = 10
    restart_backoff_seconds: int = 5

    loop_sleep_min_seconds: float = 2.0
    loop_sleep_max_seconds: float = 5.0

    start_button_selector: str = (
        "xpath=//button[.//div[normalize-space()='Start'] or normalize-space()='Start']"
    )
    chat_input_selector: str = "xpath=//textarea[@placeholder='Write a message...']"
    send_button_selector: str = "xpath=//button[normalize-space()='Send' or .//*[normalize-space()='Send']]"
    message_container_selector: str = (
        "xpath=(//div[contains(@class,'bg-base-100') and contains(@class,'rounded-2xl') and contains(@class,'flex-col')])[1]"
    )
    active_chat_marker_selector: str = "xpath=//div[contains(normalize-space(),'Match found!')]"
    looking_for_chat_selector: str = "xpath=//div[contains(normalize-space(),'Looking for someone...')]"
    press_start_continue_selector: str = "xpath=//div[contains(normalize-space(),'Press start to begin chatting.')]"
    incoming_message_selector: str = (
        "xpath=//div["
        "contains(concat(' ', normalize-space(@class), ' '), ' flex ') "
        "and contains(concat(' ', normalize-space(@class), ' '), ' justify-start ')"
        "]/div[contains(concat(' ', normalize-space(@class), ' '), ' w-fit ')]"
    )
    outgoing_message_selector: str = (
        "xpath=//div["
        "contains(concat(' ', normalize-space(@class), ' '), ' flex ') "
        "and contains(concat(' ', normalize-space(@class), ' '), ' justify-end ')"
        "]/div[contains(concat(' ', normalize-space(@class), ' '), ' w-fit ')]"
    )
    disconnect_selector: str = (
        "xpath=//div[contains(normalize-space(),'Stranger disconnected.') "
        "or contains(normalize-space(),'You have disconnected.')]"
    )
    new_chat_selector: str = (
        "xpath=//button[normalize-space()='New Chat' or normalize-space()='Start' or normalize-space()='Next' "
        "or .//div[normalize-space()='Start'] or .//div[normalize-space()='Next']]"
    )
    new_chat_confirm_selector: str = "xpath=//button[.//div[normalize-space()='Sure?'] or normalize-space()='Sure?']"
    promo_dialog_selector: str = (
        "xpath=//div[(.//div[normalize-space()='Buy A Boost!'] or .//*[normalize-space()='Buy A Boost!']) "
        "and (.//div[contains(normalize-space(),'Match *ONLY* with the hottest people on Thundr!')] "
        "or .//*[contains(normalize-space(),'Match *ONLY* with the hottest people on Thundr!')] "
        "or .//button[normalize-space()='Boost Me'])]"
    )
    promo_maybe_later_selector: str = "xpath=//button[normalize-space()='Skip' or .//span[normalize-space()='Skip']]"
    promo_close_selector: str = "xpath=//button[normalize-space()='Skip' or .//span[normalize-space()='Skip']]"
    promo_skip_wait_seconds: float = 4.0
    banned_selector: str = "xpath=//p[contains(normalize-space(),'You have been banned')]"
    swipe_gate_selector: str = "xpath=//p[contains(normalize-space(),'Swipe UP to start a NEW chat')]"
    loading_chat_selector: str = "xpath=//button[.//div[normalize-space()='Waiting'] or normalize-space()='Waiting']"
    socket_error_selector: str = (
        "xpath=//p[contains(normalize-space(),'Error connecting to socket') "
        "or contains(normalize-space(),'Error connecting to server')]"
    )
    match_error_selector: str = "xpath=//p[contains(normalize-space(),'Error connecting to match')]"

    preset_responses: list[str] = field(
        default_factory=lambda: [
            "Hey! Check out my business at example.com for awesome deals.",
            "What do you think? Visit example.com to learn more.",
            "Got questions? Head to example.com and let's chat there!",
        ]
    )
    user_ids: list[str] = field(
        default_factory=lambda: ["your_profile_id1", "your_profile_id2", "your_profile_id3"]
    )
    backup_user_ids: list[str] = field(default_factory=list)
    registered_user_ids: list[str] = field(default_factory=list)
    unregistered_user_ids: list[str] = field(default_factory=list)

    log_dir: str = "logs"
    send_first_message: bool = True
    opener_delay_min_seconds: float = 0.05
    opener_delay_max_seconds: float = 0.2
    split_opener_enabled: bool = False
    split_opener_delimiter: str = "~"
    split_opener_delay_min_seconds: float = 1.5
    split_opener_delay_max_seconds: float = 3.0
    single_message_mode: bool = False
    single_message_rotate_after_seconds: float = 12.0
    no_reply_timeout_seconds: int = 60
    no_progress_timeout_seconds: float = 180.0
    post_cta_skip: bool = True
    post_cta_delay_min_seconds: float = 4.0
    post_cta_delay_max_seconds: float = 12.0
    post_cta_ready_timeout_seconds: float = 18.0
    post_cta_local_recovery_attempts: int = 2
    respond_only_on_reply: bool = True
    reply_delay_min_seconds: float = 2.0
    reply_delay_max_seconds: float = 6.0
    press_start_max_consecutive_attempts: int = 5
    transition_instability_max_events: int = 8
    swipe_gate_recovery_cooldown_seconds: int = 20
    loading_stuck_timeout_min_seconds: float = 20.0
    loading_stuck_timeout_max_seconds: float = 30.0
    loading_recovery_max_attempts: int = 3
    looking_stuck_timeout_min_seconds: float = 12.0
    looking_stuck_timeout_max_seconds: float = 20.0
    rotate_click_grace_seconds: float = 3.0
    post_rotate_ui_retry_attempts: int = 2
    recovery_poll_seconds: float = 0.25
    active_loop_sleep_min_seconds: float = 0.15
    active_loop_sleep_max_seconds: float = 0.35
    socket_error_self_recover_seconds: float = 6.0
    socket_error_refresh_attempts: int = 2
    match_error_refresh_attempts: int = 2
    disconnect_recovery_grace_seconds: float = 8.0
    disconnect_ready_timeout_seconds: float = 18.0
    disconnect_local_recovery_attempts: int = 3

    registration_enabled: bool = True
    registration_home_url: str = "https://thundr.com"
    auto_register_backups: bool = True
    replace_proxy_for_texting: bool = False
    enforce_texting_proxy_on_chat_start: bool = False
    texting_proxy_http: str | None = None
    texting_proxy_http_list: list[str] = field(default_factory=list)
    texting_proxy_max_instances_per_proxy: int = 10
    proxy_rotation_enabled: bool = False
    texting_proxy_rotation_links: list[str] = field(default_factory=list)
    proxy_rotation_trigger_burst_count: int = 3
    proxy_rotation_trigger_burst_spacing_seconds: float = 1.0
    proxy_rotation_post_trigger_delay_seconds: float = 5.0
    proxy_rotation_verify_ip_change: bool = True
    proxy_rotation_ip_check_url: str = "https://api.ipify.org"
    proxy_rotation_ipv4_check_url: str = "https://api4.ipify.org"
    proxy_rotation_ipv6_check_url: str = "https://api6.ipify.org"
    proxy_rotation_ipv4_check_urls: list[str] = field(
        default_factory=lambda: [
            "https://api4.ipify.org",
            "https://4.ident.me",
            "https://4.tnedi.me",
        ]
    )
    proxy_rotation_ipv6_check_urls: list[str] = field(
        default_factory=lambda: [
            "https://api6.ipify.org",
            "https://6.ident.me",
            "https://6.tnedi.me",
        ]
    )
    proxy_rotation_ip_consensus_min_providers: int = 2
    proxy_rotation_verify_timeout_seconds: float = 45.0
    proxy_rotation_verify_poll_seconds: float = 3.0
    proxy_rotation_ip_check_attempts: int = 3
    proxy_rotation_ip_check_retry_delay_seconds: float = 1.5
    proxy_rotation_attempts: int = 3
    proxy_rotation_retry_cooldown_seconds: float = 15.0
    proxy_rotation_recovery_enabled: bool = True
    proxy_rotation_recovery_initial_backoff_seconds: float = 30.0
    proxy_rotation_recovery_max_backoff_seconds: float = 300.0
    proxy_rotation_resume_delay_seconds: float = 5.0
    proxy_rotation_auto_resume_quarantined_slots: bool = True
    proxy_rotation_first_resume_delay_seconds: float = 20.0
    proxy_rotation_resume_stagger_seconds: float = 8.0
    proxy_rotation_canary_enabled: bool = True
    proxy_rotation_canary_attempts_per_wave: int = 3
    proxy_rotation_canary_retry_delay_seconds: float = 15.0
    proxy_rotation_triage_survivors_on_ban: bool = True
    capacity_reconciliation_enabled: bool = True
    dynamic_backup_refill_enabled: bool = False
    target_ready_backup_count: int = 10
    delete_terminal_profiles: bool = True
    backup_pool_state_path: str = "logs/backup_pool_state.json"
    adspower_profile_template_source: str | None = None
    adspower_extension_category_id: str | None = None
    adspower_extension_category_name: str | None = None
    adspower_cookie_rpa_job_id: str | None = None
    adspower_cookie_rpa_launch_command: str | None = None
    adspower_cookie_rpa_status_command: str | None = None
    adspower_cookie_rpa_poll_seconds: float = 5.0
    adspower_cookie_rpa_require_browser_close: bool = True
    backup_refill_cookie_build_enabled: bool = True
    backup_refill_prep_concurrency: int = 1
    backup_refill_proxy_source: str = "random"
    backup_refill_block_on_create_delete_unsupported: bool = True
    backup_refill_cookie_build_command: str | None = None
    backup_refill_cookie_build_timeout_seconds: float = 1800.0
    initial_slot_start_stagger_seconds: float = 0.0
    initial_slot_start_jitter_seconds: float = 0.0
    launch_backup_on_post_rotation_match_error: bool = True
    registration_max_username_attempts: int = 5
    registration_username_prefix: str = "th_user"
    registration_username_prefixes: list[str] = field(default_factory=list)
    registration_username_suffix_min_len: int = 4
    registration_username_suffix_max_len: int = 8
    registration_username_suffix_charset: str = "0123456789Xx_"
    registration_password: str = "ThundrA1!"
    registration_password_randomize: bool = True
    registration_password_min_len: int = 10
    registration_password_max_len: int = 14
    registration_password_charset: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    registration_birth_month: int = 3
    registration_birth_day: int = 9
    registration_birth_year: int = 2000
    registration_birth_randomize: bool = True
    registration_birth_year_min: int = 1996
    registration_birth_year_max: int = 2001
    registration_action_delay_min_seconds: float = 1.0
    registration_action_delay_max_seconds: float = 3.0
    registration_wizard_visible_retries: int = 3
    registration_wizard_retry_delay_min_seconds: float = 0.4
    registration_wizard_retry_delay_max_seconds: float = 0.8
    registration_final_transition_timeout_seconds: int = 25
    registration_retryable_restart_attempts: int = 2
    registration_dead_submit_restart_threshold: int = 2
    registration_signin_url: str = "https://thundr.com/signin"
    registration_signup_url: str = "https://thundr.com/signup"
    registration_wizard_url_fragment: str = "thundr.com/register"
    registration_geo_url: str = "https://thundr.com/settings?viewMode=country"

    reg_start_selector: str = (
        "xpath=//button[@aria-label='Start Chatting' or normalize-space()='🏁 Start' or .//*[normalize-space()='🏁 Start']]"
    )
    reg_signin_page_marker_selector: str = "xpath=//button[@aria-label='Sign In Anonymously']"
    reg_signin_anon_selector: str = (
        "xpath=//button[@aria-label='Sign In Anonymously' or normalize-space()='Sign In Anonymously' "
        "or .//*[normalize-space()='Sign In Anonymously']]"
    )
    reg_signin_close_selector: str = "xpath=//button[.//*[contains(@class,'lucide-x')] or @aria-label='Close' or normalize-space()='×']"
    reg_anon_auth_page_marker_selector: str = (
        "xpath=//button[@aria-label='Create Account' and not(ancestor::*//input[@placeholder='Choose a unique username'])]"
    )
    reg_create_account_selector: str = "xpath=//button[@aria-label='Create Account' or normalize-space()='Create Account']"
    reg_signin_submit_selector: str = "xpath=//button[normalize-space()='Sign In']"
    reg_create_page_marker_selector: str = "input[placeholder='Choose a unique username']"
    reg_username_selector: str = "input[placeholder='Choose a unique username']"
    reg_password_selector: str = "input[placeholder='Choose a password']"
    reg_confirm_password_selector: str = "input[placeholder='Confirm your password'], input[aria-label='Confirm Password']"
    reg_submit_selector: str = "button[aria-label='Create Account']"
    reg_error_short_password_selector: str = "xpath=//div[contains(normalize-space(),'Password must be between 6 and 25 characters long')]"
    reg_error_password_mismatch_selector: str = "xpath=//div[contains(normalize-space(),'Passwords do not match')]"
    reg_error_username_taken_selector: str = "xpath=//div[contains(normalize-space(),'Username has already been taken')]"
    reg_ack_page_marker_selector: str = (
        "xpath=//button[@aria-label='I Agree' or normalize-space()='I Agree'] | "
        "//*[contains(normalize-space(),'You must be at least 18 years old to use Thundr')] | "
        "//a[contains(@href,'/terms')]"
    )
    reg_ack_warning_selector: str = "xpath=//*[contains(normalize-space(),'You must be at least 18 years old to use Thundr')]"
    reg_ack_agree_selector: str = "xpath=//button[@aria-label='I Agree' or normalize-space()='I Agree']"
    reg_bday_page_marker_selector: str = "xpath=//h1[normalize-space()='What Is Your Birthday?']"
    reg_bday_next_selector: str = "xpath=//button[@aria-label='Next' or normalize-space()='Next']"
    reg_bday_step_selector: str = "xpath=//p[contains(normalize-space(),'Step 1 / 3')]"
    reg_bday_month_selector: str = "xpath=(//h1[normalize-space()='What Is Your Birthday?']/following::select)[1]"
    reg_bday_day_selector: str = "xpath=(//h1[normalize-space()='What Is Your Birthday?']/following::select)[2]"
    reg_bday_year_selector: str = "xpath=(//h1[normalize-space()='What Is Your Birthday?']/following::select)[3]"
    reg_gender_page_marker_selector: str = "xpath=//h1[normalize-space()='What Is Your Sex?']"
    reg_gender_step_selector: str = "xpath=//p[contains(normalize-space(),'Step 2 / 3')]"
    reg_gender_next_selector: str = "xpath=//button[@aria-label='Next' or normalize-space()='Next']"
    reg_gender_select_selector: str = "xpath=//h1[normalize-space()='What Is Your Sex?']/following::select[1]"
    reg_gender_female_selector: str = "xpath=//label[.//p[normalize-space()='Female']]"
    reg_meet_page_marker_selector: str = "xpath=//h1[normalize-space()='Looking To Meet?']"
    reg_meet_step_selector: str = "xpath=//p[contains(normalize-space(),'Step 3 / 3')]"
    reg_meet_start_selector: str = "xpath=//button[@aria-label='Finish Registration' or normalize-space()='🏁 Start' or .//*[normalize-space()='🏁 Start']]"
    reg_meet_select_selector: str = "xpath=//h1[normalize-space()='Looking To Meet?']/following::select[1]"
    reg_meet_everyone_selector: str = "xpath=//label[.//p[normalize-space()='Everyone']]"
    reg_video_no_camera_selector: str = (
        "xpath=//div[contains(normalize-space(),\"Couldn't connect to your video device\")]"
    )
    reg_video_local_selector: str = "xpath=//video[@autoplay and @playsinline]"
    reg_video_remote_spinner_selector: str = (
        "xpath=//div[contains(@class,'rounded-2xl') and .//svg[contains(@class,'animate-spin')]]"
    )
    reg_geo_button_selector: str = (
        "xpath=//button[@aria-label='Open Country Settings' or contains(normalize-space(),'Country Filter')]"
    )
    reg_geo_select_countries_selector: str = "xpath=//button[normalize-space()='Select Countries' or .//*[normalize-space()='Select Countries']]"
    reg_geo_page_marker_selector: str = (
        "xpath=//div[contains(normalize-space(),'Country Filter (OFF)') or contains(normalize-space(),'Country Filter (') ]"
    )
    reg_geo_country_au_selector: str = "xpath=//button[contains(normalize-space(),'Australia')]"
    reg_geo_country_ca_selector: str = "xpath=//button[contains(normalize-space(),'Canada')]"
    reg_geo_country_us_selector: str = (
        "xpath=//button[contains(normalize-space(),'United States') or contains(normalize-space(),'USA')]"
    )
    reg_geo_country_uk_selector: str = (
        "xpath=//button[contains(normalize-space(),'United Kingdom') or contains(normalize-space(),'UK')]"
    )
    reg_geo_selected_count_selector: str = "xpath=//div[contains(normalize-space(),'Country Filter (')]"
    reg_geo_done_selector: str = "xpath=//button[contains(normalize-space(),'Done') and contains(normalize-space(),'Selected')]"
    reg_geo_deselect_all_selector: str = "xpath=//button[normalize-space()='Deselect All']"
    reg_home_button_selector: str = "xpath=//button[@aria-label='Home' or .//p[normalize-space()='Home']]"

    @classmethod
    def from_env(cls) -> "BotConfig":
        _load_dotenv_if_present()
        defaults = cls()
        api_key = os.getenv("ADSPOWER_API_KEY") or None
        api_base = os.getenv("ADSPOWER_API_BASE", defaults.api_base)
        target_url = os.getenv("THUNDR_TARGET_URL", defaults.target_url)

        user_ids_raw = os.getenv("ADSPOWER_USER_IDS")
        user_ids = (
            [item.strip() for item in user_ids_raw.split(",") if item.strip()]
            if user_ids_raw
            else defaults.user_ids
        )

        responses_raw = os.getenv("THUNDR_PRESET_RESPONSES")
        preset_responses = (
            [item.strip() for item in responses_raw.split("||") if item.strip()]
            if responses_raw
            else defaults.preset_responses
        )
        backup_ids_raw = os.getenv("ADSPOWER_BACKUP_USER_IDS")
        backup_user_ids = (
            [item.strip() for item in backup_ids_raw.split(",") if item.strip()]
            if backup_ids_raw
            else defaults.backup_user_ids
        )
        registered_ids_raw = os.getenv("ADSPOWER_REGISTERED_USER_IDS")
        registered_user_ids = (
            [item.strip() for item in registered_ids_raw.split(",") if item.strip()]
            if registered_ids_raw
            else defaults.registered_user_ids
        )
        unregistered_ids_raw = os.getenv("ADSPOWER_UNREGISTERED_USER_IDS")
        unregistered_user_ids = (
            [item.strip() for item in unregistered_ids_raw.split(",") if item.strip()]
            if unregistered_ids_raw
            else defaults.unregistered_user_ids
        )
        username_prefixes_raw = os.getenv("BOT_REG_USERNAME_PREFIXES")
        registration_username_prefixes = (
            [item.strip() for item in username_prefixes_raw.split(",") if item.strip()]
            if username_prefixes_raw
            else defaults.registration_username_prefixes
        )
        texting_proxy_http_list_raw = os.getenv("BOT_TEXTING_PROXY_HTTP_LIST")
        texting_proxy_http_list = (
            [item.strip() for item in texting_proxy_http_list_raw.split("||") if item.strip()]
            if texting_proxy_http_list_raw
            else defaults.texting_proxy_http_list
        )
        texting_proxy_rotation_links_raw = os.getenv("BOT_TEXTING_PROXY_ROTATION_LINKS")
        texting_proxy_rotation_links = (
            [item.strip() for item in texting_proxy_rotation_links_raw.split("||") if item.strip()]
            if texting_proxy_rotation_links_raw
            else defaults.texting_proxy_rotation_links
        )
        proxy_rotation_ipv4_check_urls_raw = os.getenv("BOT_PROXY_ROTATION_IPV4_CHECK_URLS")
        proxy_rotation_ipv4_check_urls = (
            [item.strip() for item in proxy_rotation_ipv4_check_urls_raw.split("||") if item.strip()]
            if proxy_rotation_ipv4_check_urls_raw
            else defaults.proxy_rotation_ipv4_check_urls
        )
        proxy_rotation_ipv6_check_urls_raw = os.getenv("BOT_PROXY_ROTATION_IPV6_CHECK_URLS")
        proxy_rotation_ipv6_check_urls = (
            [item.strip() for item in proxy_rotation_ipv6_check_urls_raw.split("||") if item.strip()]
            if proxy_rotation_ipv6_check_urls_raw
            else defaults.proxy_rotation_ipv6_check_urls
        )

        return cls(
            api_base=api_base,
            api_key=api_key,
            target_url=target_url,
            user_ids=user_ids,
            backup_user_ids=backup_user_ids,
            registered_user_ids=registered_user_ids,
            unregistered_user_ids=unregistered_user_ids,
            preset_responses=preset_responses,
            element_wait_seconds=int(os.getenv("BOT_ELEMENT_WAIT_SECONDS", "12")),
            api_timeout_seconds=int(os.getenv("BOT_API_TIMEOUT_SECONDS", "15")),
            api_retries=int(os.getenv("BOT_API_RETRIES", "3")),
            max_session_restarts=int(os.getenv("BOT_MAX_SESSION_RESTARTS", "3")),
            max_consecutive_loop_errors=int(
                os.getenv("BOT_MAX_CONSECUTIVE_LOOP_ERRORS", "10")
            ),
            restart_backoff_seconds=int(os.getenv("BOT_RESTART_BACKOFF_SECONDS", "5")),
            loop_sleep_min_seconds=float(os.getenv("BOT_LOOP_SLEEP_MIN_SECONDS", "2")),
            loop_sleep_max_seconds=float(os.getenv("BOT_LOOP_SLEEP_MAX_SECONDS", "5")),
            start_button_selector=os.getenv(
                "BOT_START_BUTTON_SELECTOR", defaults.start_button_selector
            ),
            chat_input_selector=os.getenv("BOT_CHAT_INPUT_SELECTOR", defaults.chat_input_selector),
            send_button_selector=os.getenv("BOT_SEND_BUTTON_SELECTOR", defaults.send_button_selector),
            message_container_selector=os.getenv(
                "BOT_MESSAGE_CONTAINER_SELECTOR", defaults.message_container_selector
            ),
            active_chat_marker_selector=os.getenv(
                "BOT_ACTIVE_CHAT_MARKER_SELECTOR", defaults.active_chat_marker_selector
            ),
            looking_for_chat_selector=os.getenv(
                "BOT_LOOKING_FOR_CHAT_SELECTOR", defaults.looking_for_chat_selector
            ),
            press_start_continue_selector=os.getenv(
                "BOT_PRESS_START_CONTINUE_SELECTOR", defaults.press_start_continue_selector
            ),
            incoming_message_selector=os.getenv(
                "BOT_INCOMING_MESSAGE_SELECTOR", defaults.incoming_message_selector
            ),
            outgoing_message_selector=os.getenv(
                "BOT_OUTGOING_MESSAGE_SELECTOR", defaults.outgoing_message_selector
            ),
            disconnect_selector=os.getenv(
                "BOT_DISCONNECT_SELECTOR", defaults.disconnect_selector
            ),
            new_chat_selector=os.getenv("BOT_NEW_CHAT_SELECTOR", defaults.new_chat_selector),
            new_chat_confirm_selector=os.getenv(
                "BOT_NEW_CHAT_CONFIRM_SELECTOR", defaults.new_chat_confirm_selector
            ),
            promo_dialog_selector=os.getenv(
                "BOT_PROMO_DIALOG_SELECTOR", defaults.promo_dialog_selector
            ),
            promo_maybe_later_selector=os.getenv(
                "BOT_PROMO_MAYBE_LATER_SELECTOR", defaults.promo_maybe_later_selector
            ),
            promo_close_selector=os.getenv(
                "BOT_PROMO_CLOSE_SELECTOR", defaults.promo_close_selector
            ),
            promo_skip_wait_seconds=float(
                os.getenv("BOT_PROMO_SKIP_WAIT_SECONDS", str(defaults.promo_skip_wait_seconds))
            ),
            banned_selector=os.getenv("BOT_BANNED_SELECTOR", defaults.banned_selector),
            swipe_gate_selector=os.getenv("BOT_SWIPE_GATE_SELECTOR", defaults.swipe_gate_selector),
            loading_chat_selector=os.getenv(
                "BOT_LOADING_CHAT_SELECTOR", defaults.loading_chat_selector
            ),
            socket_error_selector=os.getenv(
                "BOT_SOCKET_ERROR_SELECTOR", defaults.socket_error_selector
            ),
            match_error_selector=os.getenv(
                "BOT_MATCH_ERROR_SELECTOR", defaults.match_error_selector
            ),
            log_dir=os.getenv("BOT_LOG_DIR", "logs"),
            send_first_message=os.getenv("BOT_SEND_FIRST_MESSAGE", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            opener_delay_min_seconds=float(
                os.getenv("BOT_OPENER_DELAY_MIN_SECONDS", "0.05")
            ),
            opener_delay_max_seconds=float(
                os.getenv("BOT_OPENER_DELAY_MAX_SECONDS", "0.2")
            ),
            split_opener_enabled=os.getenv("BOT_SPLIT_OPENER_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            split_opener_delimiter=os.getenv(
                "BOT_SPLIT_OPENER_DELIMITER", defaults.split_opener_delimiter
            ),
            split_opener_delay_min_seconds=float(
                os.getenv("BOT_SPLIT_OPENER_DELAY_MIN_SECONDS", "1.5")
            ),
            split_opener_delay_max_seconds=float(
                os.getenv("BOT_SPLIT_OPENER_DELAY_MAX_SECONDS", "3.0")
            ),
            single_message_mode=os.getenv("BOT_SINGLE_MESSAGE_MODE", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            single_message_rotate_after_seconds=float(
                os.getenv("BOT_SINGLE_MESSAGE_ROTATE_AFTER_SECONDS", "12")
            ),
            no_reply_timeout_seconds=int(os.getenv("BOT_NO_REPLY_TIMEOUT_SECONDS", "60")),
            no_progress_timeout_seconds=float(
                os.getenv("BOT_NO_PROGRESS_TIMEOUT_SECONDS", "180")
            ),
            post_cta_skip=os.getenv("BOT_POST_CTA_SKIP", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            post_cta_delay_min_seconds=float(os.getenv("BOT_POST_CTA_DELAY_MIN_SECONDS", "4")),
            post_cta_delay_max_seconds=float(os.getenv("BOT_POST_CTA_DELAY_MAX_SECONDS", "12")),
            post_cta_ready_timeout_seconds=float(
                os.getenv("BOT_POST_CTA_READY_TIMEOUT_SECONDS", "18")
            ),
            post_cta_local_recovery_attempts=int(
                os.getenv("BOT_POST_CTA_LOCAL_RECOVERY_ATTEMPTS", "2")
            ),
            respond_only_on_reply=os.getenv("BOT_RESPOND_ONLY_ON_REPLY", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            reply_delay_min_seconds=float(os.getenv("BOT_REPLY_DELAY_MIN_SECONDS", "2")),
            reply_delay_max_seconds=float(os.getenv("BOT_REPLY_DELAY_MAX_SECONDS", "6")),
            press_start_max_consecutive_attempts=int(
                os.getenv("BOT_PRESS_START_MAX_CONSECUTIVE_ATTEMPTS", "5")
            ),
            transition_instability_max_events=int(
                os.getenv("BOT_TRANSITION_INSTABILITY_MAX_EVENTS", "8")
            ),
            swipe_gate_recovery_cooldown_seconds=int(
                os.getenv("BOT_SWIPE_GATE_RECOVERY_COOLDOWN_SECONDS", "20")
            ),
            loading_stuck_timeout_min_seconds=float(
                os.getenv("BOT_LOADING_STUCK_TIMEOUT_MIN_SECONDS", "20")
            ),
            loading_stuck_timeout_max_seconds=float(
                os.getenv("BOT_LOADING_STUCK_TIMEOUT_MAX_SECONDS", "30")
            ),
            loading_recovery_max_attempts=int(
                os.getenv("BOT_LOADING_RECOVERY_MAX_ATTEMPTS", "3")
            ),
            looking_stuck_timeout_min_seconds=float(
                os.getenv("BOT_LOOKING_STUCK_TIMEOUT_MIN_SECONDS", "12")
            ),
            looking_stuck_timeout_max_seconds=float(
                os.getenv("BOT_LOOKING_STUCK_TIMEOUT_MAX_SECONDS", "20")
            ),
            rotate_click_grace_seconds=float(
                os.getenv("BOT_ROTATE_CLICK_GRACE_SECONDS", "3")
            ),
            post_rotate_ui_retry_attempts=int(
                os.getenv("BOT_POST_ROTATE_UI_RETRY_ATTEMPTS", "2")
            ),
            recovery_poll_seconds=float(
                os.getenv("BOT_RECOVERY_POLL_SECONDS", "0.25")
            ),
            active_loop_sleep_min_seconds=float(
                os.getenv("BOT_ACTIVE_LOOP_SLEEP_MIN_SECONDS", "0.15")
            ),
            active_loop_sleep_max_seconds=float(
                os.getenv("BOT_ACTIVE_LOOP_SLEEP_MAX_SECONDS", "0.35")
            ),
            socket_error_self_recover_seconds=float(
                os.getenv("BOT_SOCKET_ERROR_SELF_RECOVER_SECONDS", "6")
            ),
            socket_error_refresh_attempts=int(
                os.getenv("BOT_SOCKET_ERROR_REFRESH_ATTEMPTS", "2")
            ),
            match_error_refresh_attempts=int(
                os.getenv("BOT_MATCH_ERROR_REFRESH_ATTEMPTS", "2")
            ),
            disconnect_recovery_grace_seconds=float(
                os.getenv("BOT_DISCONNECT_RECOVERY_GRACE_SECONDS", "8")
            ),
            disconnect_ready_timeout_seconds=float(
                os.getenv("BOT_DISCONNECT_READY_TIMEOUT_SECONDS", "18")
            ),
            disconnect_local_recovery_attempts=int(
                os.getenv("BOT_DISCONNECT_LOCAL_RECOVERY_ATTEMPTS", "3")
            ),
            registration_enabled=os.getenv("BOT_REGISTRATION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            registration_home_url=os.getenv("BOT_REGISTRATION_HOME_URL", defaults.registration_home_url),
            auto_register_backups=os.getenv("BOT_AUTO_REGISTER_BACKUPS", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            replace_proxy_for_texting=os.getenv("BOT_REPLACE_PROXY_FOR_TEXTING", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            enforce_texting_proxy_on_chat_start=os.getenv(
                "BOT_ENFORCE_TEXTING_PROXY_ON_CHAT_START", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            texting_proxy_http=(os.getenv("BOT_TEXTING_PROXY_HTTP") or None),
            texting_proxy_http_list=texting_proxy_http_list,
            texting_proxy_max_instances_per_proxy=int(
                os.getenv("BOT_TEXTING_PROXY_MAX_INSTANCES_PER_PROXY", "10")
            ),
            proxy_rotation_enabled=os.getenv("BOT_PROXY_ROTATION_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            texting_proxy_rotation_links=texting_proxy_rotation_links,
            proxy_rotation_trigger_burst_count=int(
                os.getenv("BOT_PROXY_ROTATION_TRIGGER_BURST_COUNT", "3")
            ),
            proxy_rotation_trigger_burst_spacing_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_TRIGGER_BURST_SPACING_SECONDS", "1")
            ),
            proxy_rotation_post_trigger_delay_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_POST_TRIGGER_DELAY_SECONDS", "5")
            ),
            proxy_rotation_verify_ip_change=os.getenv(
                "BOT_PROXY_ROTATION_VERIFY_IP_CHANGE", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            proxy_rotation_ip_check_url=os.getenv(
                "BOT_PROXY_ROTATION_IP_CHECK_URL", defaults.proxy_rotation_ip_check_url
            ),
            proxy_rotation_ipv4_check_url=os.getenv(
                "BOT_PROXY_ROTATION_IPV4_CHECK_URL",
                os.getenv("BOT_PROXY_ROTATION_IP_CHECK_URL", defaults.proxy_rotation_ipv4_check_url),
            ),
            proxy_rotation_ipv6_check_url=os.getenv(
                "BOT_PROXY_ROTATION_IPV6_CHECK_URL",
                defaults.proxy_rotation_ipv6_check_url,
            ),
            proxy_rotation_ipv4_check_urls=proxy_rotation_ipv4_check_urls,
            proxy_rotation_ipv6_check_urls=proxy_rotation_ipv6_check_urls,
            proxy_rotation_ip_consensus_min_providers=int(
                os.getenv("BOT_PROXY_ROTATION_IP_CONSENSUS_MIN_PROVIDERS", "2")
            ),
            proxy_rotation_verify_timeout_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_VERIFY_TIMEOUT_SECONDS", "45")
            ),
            proxy_rotation_verify_poll_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_VERIFY_POLL_SECONDS", "3")
            ),
            proxy_rotation_ip_check_attempts=int(
                os.getenv("BOT_PROXY_ROTATION_IP_CHECK_ATTEMPTS", "3")
            ),
            proxy_rotation_ip_check_retry_delay_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_IP_CHECK_RETRY_DELAY_SECONDS", "1.5")
            ),
            proxy_rotation_attempts=int(
                os.getenv("BOT_PROXY_ROTATION_ATTEMPTS", "3")
            ),
            proxy_rotation_retry_cooldown_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_RETRY_COOLDOWN_SECONDS", "15")
            ),
            proxy_rotation_recovery_enabled=os.getenv(
                "BOT_PROXY_ROTATION_RECOVERY_ENABLED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            proxy_rotation_recovery_initial_backoff_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_RECOVERY_INITIAL_BACKOFF_SECONDS", "30")
            ),
            proxy_rotation_recovery_max_backoff_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_RECOVERY_MAX_BACKOFF_SECONDS", "300")
            ),
            proxy_rotation_resume_delay_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_RESUME_DELAY_SECONDS", "5")
            ),
            proxy_rotation_auto_resume_quarantined_slots=os.getenv(
                "BOT_PROXY_ROTATION_AUTO_RESUME_QUARANTINED_SLOTS", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            proxy_rotation_first_resume_delay_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_FIRST_RESUME_DELAY_SECONDS", "20")
            ),
            proxy_rotation_resume_stagger_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_RESUME_STAGGER_SECONDS", "8")
            ),
            proxy_rotation_canary_enabled=os.getenv(
                "BOT_PROXY_ROTATION_CANARY_ENABLED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            proxy_rotation_canary_attempts_per_wave=int(
                os.getenv("BOT_PROXY_ROTATION_CANARY_ATTEMPTS_PER_WAVE", "3")
            ),
            proxy_rotation_canary_retry_delay_seconds=float(
                os.getenv("BOT_PROXY_ROTATION_CANARY_RETRY_DELAY_SECONDS", "15")
            ),
            proxy_rotation_triage_survivors_on_ban=os.getenv(
                "BOT_PROXY_ROTATION_TRIAGE_SURVIVORS_ON_BAN", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            capacity_reconciliation_enabled=os.getenv(
                "BOT_ENABLE_CAPACITY_RECONCILIATION", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            dynamic_backup_refill_enabled=os.getenv(
                "BOT_DYNAMIC_BACKUP_REFILL_ENABLED", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            target_ready_backup_count=max(
                0,
                int(
                    os.getenv(
                        "BOT_TARGET_READY_BACKUP_COUNT",
                        str(defaults.target_ready_backup_count),
                    )
                ),
            ),
            delete_terminal_profiles=os.getenv(
                "BOT_DELETE_TERMINAL_PROFILES",
                "true" if defaults.delete_terminal_profiles else "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            backup_pool_state_path=os.getenv(
                "BOT_BACKUP_POOL_STATE_PATH", defaults.backup_pool_state_path
            ),
            adspower_profile_template_source=(
                os.getenv("BOT_ADSPOWER_PROFILE_TEMPLATE_SOURCE")
                or defaults.adspower_profile_template_source
            ),
            adspower_extension_category_id=(
                os.getenv("BOT_ADSPOWER_EXTENSION_CATEGORY_ID")
                or defaults.adspower_extension_category_id
            ),
            adspower_extension_category_name=(
                os.getenv("BOT_ADSPOWER_EXTENSION_CATEGORY_NAME")
                or defaults.adspower_extension_category_name
            ),
            adspower_cookie_rpa_job_id=(
                os.getenv("BOT_ADSPOWER_COOKIE_RPA_JOB_ID")
                or defaults.adspower_cookie_rpa_job_id
            ),
            adspower_cookie_rpa_launch_command=(
                os.getenv("BOT_ADSPOWER_COOKIE_RPA_LAUNCH_COMMAND")
                or defaults.adspower_cookie_rpa_launch_command
            ),
            adspower_cookie_rpa_status_command=(
                os.getenv("BOT_ADSPOWER_COOKIE_RPA_STATUS_COMMAND")
                or defaults.adspower_cookie_rpa_status_command
            ),
            adspower_cookie_rpa_poll_seconds=float(
                os.getenv(
                    "BOT_ADSPOWER_COOKIE_RPA_POLL_SECONDS",
                    str(defaults.adspower_cookie_rpa_poll_seconds),
                )
            ),
            adspower_cookie_rpa_require_browser_close=os.getenv(
                "BOT_ADSPOWER_COOKIE_RPA_REQUIRE_BROWSER_CLOSE", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            backup_refill_cookie_build_enabled=os.getenv(
                "BOT_BACKUP_REFILL_COOKIE_BUILD_ENABLED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            backup_refill_prep_concurrency=int(
                os.getenv(
                    "BOT_BACKUP_REFILL_PREP_CONCURRENCY",
                    str(defaults.backup_refill_prep_concurrency),
                )
            ),
            backup_refill_proxy_source=os.getenv(
                "BOT_BACKUP_REFILL_PROXY_SOURCE", defaults.backup_refill_proxy_source
            ),
            backup_refill_block_on_create_delete_unsupported=os.getenv(
                "BOT_BACKUP_REFILL_BLOCK_ON_CREATE_DELETE_UNSUPPORTED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            backup_refill_cookie_build_command=(
                os.getenv("BOT_BACKUP_REFILL_COOKIE_BUILD_COMMAND")
                or defaults.backup_refill_cookie_build_command
            ),
            backup_refill_cookie_build_timeout_seconds=float(
                os.getenv(
                    "BOT_BACKUP_REFILL_COOKIE_BUILD_TIMEOUT_SECONDS",
                    str(defaults.backup_refill_cookie_build_timeout_seconds),
                )
            ),
            initial_slot_start_stagger_seconds=float(
                os.getenv("BOT_INITIAL_SLOT_START_STAGGER_SECONDS", "0")
            ),
            initial_slot_start_jitter_seconds=float(
                os.getenv("BOT_INITIAL_SLOT_START_JITTER_SECONDS", "0")
            ),
            launch_backup_on_post_rotation_match_error=os.getenv(
                "BOT_LAUNCH_BACKUP_ON_POST_ROTATION_MATCH_ERROR", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            registration_max_username_attempts=int(
                os.getenv("BOT_REG_MAX_USERNAME_ATTEMPTS", str(defaults.registration_max_username_attempts))
            ),
            registration_username_prefix=os.getenv(
                "BOT_REG_USERNAME_PREFIX", defaults.registration_username_prefix
            ),
            registration_username_prefixes=registration_username_prefixes,
            registration_username_suffix_min_len=int(
                os.getenv(
                    "BOT_REG_USERNAME_SUFFIX_MIN_LEN",
                    str(defaults.registration_username_suffix_min_len),
                )
            ),
            registration_username_suffix_max_len=int(
                os.getenv(
                    "BOT_REG_USERNAME_SUFFIX_MAX_LEN",
                    str(defaults.registration_username_suffix_max_len),
                )
            ),
            registration_username_suffix_charset=os.getenv(
                "BOT_REG_USERNAME_SUFFIX_CHARSET",
                defaults.registration_username_suffix_charset,
            ),
            registration_password=os.getenv("BOT_REG_PASSWORD", defaults.registration_password),
            registration_password_randomize=os.getenv(
                "BOT_REG_PASSWORD_RANDOMIZE", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            registration_password_min_len=int(
                os.getenv(
                    "BOT_REG_PASSWORD_MIN_LEN",
                    str(defaults.registration_password_min_len),
                )
            ),
            registration_password_max_len=int(
                os.getenv(
                    "BOT_REG_PASSWORD_MAX_LEN",
                    str(defaults.registration_password_max_len),
                )
            ),
            registration_password_charset=os.getenv(
                "BOT_REG_PASSWORD_CHARSET",
                defaults.registration_password_charset,
            ),
            registration_birth_month=int(
                os.getenv("BOT_REG_BIRTH_MONTH", str(defaults.registration_birth_month))
            ),
            registration_birth_day=int(
                os.getenv("BOT_REG_BIRTH_DAY", str(defaults.registration_birth_day))
            ),
            registration_birth_year=int(
                os.getenv("BOT_REG_BIRTH_YEAR", str(defaults.registration_birth_year))
            ),
            registration_birth_randomize=os.getenv(
                "BOT_REG_BIRTH_RANDOMIZE", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            registration_birth_year_min=int(
                os.getenv(
                    "BOT_REG_BIRTH_YEAR_MIN",
                    str(defaults.registration_birth_year_min),
                )
            ),
            registration_birth_year_max=int(
                os.getenv(
                    "BOT_REG_BIRTH_YEAR_MAX",
                    str(defaults.registration_birth_year_max),
                )
            ),
            registration_action_delay_min_seconds=float(
                os.getenv(
                    "BOT_REG_ACTION_DELAY_MIN_SECONDS",
                    str(defaults.registration_action_delay_min_seconds),
                )
            ),
            registration_action_delay_max_seconds=float(
                os.getenv(
                    "BOT_REG_ACTION_DELAY_MAX_SECONDS",
                    str(defaults.registration_action_delay_max_seconds),
                )
            ),
            registration_wizard_visible_retries=int(
                os.getenv(
                    "BOT_REG_WIZARD_VISIBLE_RETRIES",
                    str(defaults.registration_wizard_visible_retries),
                )
            ),
            registration_wizard_retry_delay_min_seconds=float(
                os.getenv(
                    "BOT_REG_WIZARD_RETRY_DELAY_MIN_SECONDS",
                    str(defaults.registration_wizard_retry_delay_min_seconds),
                )
            ),
            registration_wizard_retry_delay_max_seconds=float(
                os.getenv(
                    "BOT_REG_WIZARD_RETRY_DELAY_MAX_SECONDS",
                    str(defaults.registration_wizard_retry_delay_max_seconds),
                )
            ),
            registration_final_transition_timeout_seconds=int(
                os.getenv(
                    "BOT_REG_FINAL_TRANSITION_TIMEOUT_SECONDS",
                    str(defaults.registration_final_transition_timeout_seconds),
                )
            ),
            registration_retryable_restart_attempts=int(
                os.getenv(
                    "BOT_REG_RETRYABLE_RESTART_ATTEMPTS",
                    str(defaults.registration_retryable_restart_attempts),
                )
            ),
            registration_dead_submit_restart_threshold=int(
                os.getenv(
                    "BOT_REG_DEAD_SUBMIT_RESTART_THRESHOLD",
                    str(defaults.registration_dead_submit_restart_threshold),
                )
            ),
            registration_signin_url=os.getenv(
                "BOT_REGISTRATION_SIGNIN_URL", defaults.registration_signin_url
            ),
            registration_signup_url=os.getenv(
                "BOT_REGISTRATION_SIGNUP_URL", defaults.registration_signup_url
            ),
            registration_wizard_url_fragment=os.getenv(
                "BOT_REGISTRATION_WIZARD_URL_FRAGMENT", defaults.registration_wizard_url_fragment
            ),
            registration_geo_url=os.getenv(
                "BOT_REGISTRATION_GEO_URL", defaults.registration_geo_url
            ),
            reg_start_selector=os.getenv("BOT_REG_START_SELECTOR", defaults.reg_start_selector),
            reg_signin_page_marker_selector=os.getenv(
                "BOT_REG_SIGNIN_PAGE_MARKER_SELECTOR", defaults.reg_signin_page_marker_selector
            ),
            reg_signin_anon_selector=os.getenv(
                "BOT_REG_SIGNIN_ANON_SELECTOR", defaults.reg_signin_anon_selector
            ),
            reg_signin_close_selector=os.getenv(
                "BOT_REG_SIGNIN_CLOSE_SELECTOR", defaults.reg_signin_close_selector
            ),
            reg_anon_auth_page_marker_selector=os.getenv(
                "BOT_REG_ANON_AUTH_PAGE_MARKER_SELECTOR",
                defaults.reg_anon_auth_page_marker_selector,
            ),
            reg_create_account_selector=os.getenv(
                "BOT_REG_CREATE_ACCOUNT_SELECTOR", defaults.reg_create_account_selector
            ),
            reg_signin_submit_selector=os.getenv(
                "BOT_REG_SIGNIN_SUBMIT_SELECTOR", defaults.reg_signin_submit_selector
            ),
            reg_create_page_marker_selector=os.getenv(
                "BOT_REG_CREATE_PAGE_MARKER_SELECTOR", defaults.reg_create_page_marker_selector
            ),
            reg_username_selector=os.getenv("BOT_REG_USERNAME_SELECTOR", defaults.reg_username_selector),
            reg_password_selector=os.getenv("BOT_REG_PASSWORD_SELECTOR", defaults.reg_password_selector),
            reg_confirm_password_selector=os.getenv(
                "BOT_REG_CONFIRM_PASSWORD_SELECTOR", defaults.reg_confirm_password_selector
            ),
            reg_submit_selector=os.getenv("BOT_REG_SUBMIT_SELECTOR", defaults.reg_submit_selector),
            reg_error_short_password_selector=os.getenv(
                "BOT_REG_ERROR_SHORT_PASSWORD_SELECTOR", defaults.reg_error_short_password_selector
            ),
            reg_error_password_mismatch_selector=os.getenv(
                "BOT_REG_ERROR_PASSWORD_MISMATCH_SELECTOR",
                defaults.reg_error_password_mismatch_selector,
            ),
            reg_error_username_taken_selector=os.getenv(
                "BOT_REG_ERROR_USERNAME_TAKEN_SELECTOR",
                defaults.reg_error_username_taken_selector,
            ),
            reg_ack_page_marker_selector=os.getenv(
                "BOT_REG_ACK_PAGE_MARKER_SELECTOR", defaults.reg_ack_page_marker_selector
            ),
            reg_ack_warning_selector=os.getenv(
                "BOT_REG_ACK_WARNING_SELECTOR", defaults.reg_ack_warning_selector
            ),
            reg_ack_agree_selector=os.getenv(
                "BOT_REG_ACK_AGREE_SELECTOR", defaults.reg_ack_agree_selector
            ),
            reg_bday_page_marker_selector=os.getenv(
                "BOT_REG_BDAY_PAGE_MARKER_SELECTOR", defaults.reg_bday_page_marker_selector
            ),
            reg_bday_next_selector=os.getenv(
                "BOT_REG_BDAY_NEXT_SELECTOR", defaults.reg_bday_next_selector
            ),
            reg_bday_step_selector=os.getenv(
                "BOT_REG_BDAY_STEP_SELECTOR", defaults.reg_bday_step_selector
            ),
            reg_bday_month_selector=os.getenv(
                "BOT_REG_BDAY_MONTH_SELECTOR", defaults.reg_bday_month_selector
            ),
            reg_bday_day_selector=os.getenv(
                "BOT_REG_BDAY_DAY_SELECTOR", defaults.reg_bday_day_selector
            ),
            reg_bday_year_selector=os.getenv(
                "BOT_REG_BDAY_YEAR_SELECTOR", defaults.reg_bday_year_selector
            ),
            reg_gender_page_marker_selector=os.getenv(
                "BOT_REG_GENDER_PAGE_MARKER_SELECTOR", defaults.reg_gender_page_marker_selector
            ),
            reg_gender_step_selector=os.getenv(
                "BOT_REG_GENDER_STEP_SELECTOR", defaults.reg_gender_step_selector
            ),
            reg_gender_next_selector=os.getenv(
                "BOT_REG_GENDER_NEXT_SELECTOR", defaults.reg_gender_next_selector
            ),
            reg_gender_select_selector=os.getenv(
                "BOT_REG_GENDER_SELECT_SELECTOR", defaults.reg_gender_select_selector
            ),
            reg_gender_female_selector=os.getenv(
                "BOT_REG_GENDER_FEMALE_SELECTOR", defaults.reg_gender_female_selector
            ),
            reg_meet_page_marker_selector=os.getenv(
                "BOT_REG_MEET_PAGE_MARKER_SELECTOR", defaults.reg_meet_page_marker_selector
            ),
            reg_meet_step_selector=os.getenv(
                "BOT_REG_MEET_STEP_SELECTOR", defaults.reg_meet_step_selector
            ),
            reg_meet_start_selector=os.getenv(
                "BOT_REG_MEET_START_SELECTOR", defaults.reg_meet_start_selector
            ),
            reg_meet_select_selector=os.getenv(
                "BOT_REG_MEET_SELECT_SELECTOR", defaults.reg_meet_select_selector
            ),
            reg_meet_everyone_selector=os.getenv(
                "BOT_REG_MEET_EVERYONE_SELECTOR", defaults.reg_meet_everyone_selector
            ),
            reg_video_no_camera_selector=os.getenv(
                "BOT_REG_VIDEO_NO_CAMERA_SELECTOR", defaults.reg_video_no_camera_selector
            ),
            reg_video_local_selector=os.getenv(
                "BOT_REG_VIDEO_LOCAL_SELECTOR", defaults.reg_video_local_selector
            ),
            reg_video_remote_spinner_selector=os.getenv(
                "BOT_REG_VIDEO_REMOTE_SPINNER_SELECTOR", defaults.reg_video_remote_spinner_selector
            ),
            reg_geo_button_selector=os.getenv(
                "BOT_REG_GEO_BUTTON_SELECTOR", defaults.reg_geo_button_selector
            ),
            reg_geo_select_countries_selector=os.getenv(
                "BOT_REG_GEO_SELECT_COUNTRIES_SELECTOR", defaults.reg_geo_select_countries_selector
            ),
            reg_geo_page_marker_selector=os.getenv(
                "BOT_REG_GEO_PAGE_MARKER_SELECTOR", defaults.reg_geo_page_marker_selector
            ),
            reg_geo_country_au_selector=os.getenv(
                "BOT_REG_GEO_COUNTRY_AU_SELECTOR", defaults.reg_geo_country_au_selector
            ),
            reg_geo_country_ca_selector=os.getenv(
                "BOT_REG_GEO_COUNTRY_CA_SELECTOR", defaults.reg_geo_country_ca_selector
            ),
            reg_geo_country_us_selector=os.getenv(
                "BOT_REG_GEO_COUNTRY_US_SELECTOR", defaults.reg_geo_country_us_selector
            ),
            reg_geo_country_uk_selector=os.getenv(
                "BOT_REG_GEO_COUNTRY_UK_SELECTOR", defaults.reg_geo_country_uk_selector
            ),
            reg_geo_selected_count_selector=os.getenv(
                "BOT_REG_GEO_SELECTED_COUNT_SELECTOR",
                defaults.reg_geo_selected_count_selector,
            ),
            reg_geo_done_selector=os.getenv(
                "BOT_REG_GEO_DONE_SELECTOR", defaults.reg_geo_done_selector
            ),
            reg_geo_deselect_all_selector=os.getenv(
                "BOT_REG_GEO_DESELECT_ALL_SELECTOR", defaults.reg_geo_deselect_all_selector
            ),
            reg_home_button_selector=os.getenv(
                "BOT_REG_HOME_BUTTON_SELECTOR", defaults.reg_home_button_selector
            ),
        )


@dataclass(frozen=True)
class RuntimeContext:
    run_id: str
    session_id: int
    user_id: str


@dataclass(frozen=True)
class SessionResult:
    status: SessionStatus
    attempts: int
    fatal_error: str | None = None
    chatted: bool = False
    opener_sends: int = 0
    cta_sends: int = 0
