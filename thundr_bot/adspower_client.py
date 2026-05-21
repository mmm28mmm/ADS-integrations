from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from thundr_bot.config import BotConfig


@dataclass(frozen=True)
class BrowserSession:
    webdriver_path: str
    debugger_address: str
    user_id: str


class AdsPowerCapabilityError(RuntimeError):
    """Raised when the local AdsPower API does not expose a requested capability."""


class AdsPowerClient:
    def __init__(self, config: BotConfig):
        self.config = config

    def get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _api_request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        retries: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        retries = retries if retries is not None else self.config.api_retries
        timeout = timeout if timeout is not None else self.config.api_timeout_seconds
        params = params or {}
        url = f"{self.config.api_base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = requests.request(
                    method.upper(),
                    url,
                    headers=self.get_headers(),
                    json=payload,
                    timeout=timeout,
                )
                try:
                    response.raise_for_status()
                except requests.HTTPError as error:
                    status_code = response.status_code
                    if status_code in {404, 405, 410, 501}:
                        raise AdsPowerCapabilityError(
                            f"AdsPower Local API endpoint unsupported: {path}"
                        ) from error
                    raise
                return response.json()
            except AdsPowerCapabilityError:
                raise
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt < retries:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise RuntimeError(
                    f"API request failed after {retries} attempts: {url}"
                ) from last_error

        raise RuntimeError("Unreachable API code path")

    def api_get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        retries: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._api_request_json(
            "GET",
            path,
            params=params,
            retries=retries,
            timeout=timeout,
        )

    def api_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        retries: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._api_request_json(
            "POST",
            path,
            payload=payload,
            retries=retries,
            timeout=timeout,
        )

    def list_extension_categories(self) -> list[dict[str, Any]]:
        candidate_requests = (
            ("GET", "/api/v2/category/list", None),
            ("GET", "/api/v1/user/sys_app_cate/list", None),
            ("GET", "/api/v1/user/sys_app_cate/list", {"page": 1, "limit": 100}),
            ("POST", "/api/v1/user/sys_app_cate/list", {}),
        )
        last_error: Exception | None = None
        for method, path, payload in candidate_requests:
            try:
                if method == "GET":
                    response = self.api_get_json(path, payload, retries=1, timeout=10)
                else:
                    response = self.api_post_json(path, payload or {}, retries=1, timeout=10)
            except AdsPowerCapabilityError as error:
                last_error = error
                continue

            if response.get("code") != 0:
                raise RuntimeError(
                    f"Failed to query AdsPower extension categories: {response.get('msg')}"
                )

            data = response.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                for key in ("list", "data", "items"):
                    value = data.get(key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
            return []

        if last_error is not None:
            raise last_error
        raise AdsPowerCapabilityError("AdsPower Local API extension-category list endpoint is unsupported")

    def resolve_extension_category_id(
        self,
        *,
        category_id: str | None = None,
        category_name: str | None = None,
    ) -> str | None:
        resolved_id = str(category_id or "").strip()
        if resolved_id:
            return resolved_id

        target_name = str(category_name or "").strip().lower()
        if not target_name:
            return None

        categories = self.list_extension_categories()
        for category in categories:
            candidate_name = str(
                category.get("category_name")
                or category.get("cate_name")
                or category.get("group_name")
                or category.get("name")
                or category.get("title")
                or ""
            ).strip().lower()
            if candidate_name != target_name:
                continue
            candidate_id = str(
                category.get("category_id")
                or category.get("cate_id")
                or category.get("id")
                or category.get("sys_app_cate_id")
                or ""
            ).strip()
            if candidate_id:
                return candidate_id

        raise RuntimeError(
            f"AdsPower extension category named {category_name!r} was not found"
        )

    def start_browser(self, user_id: str) -> BrowserSession:
        payload = self.api_get_json("/api/v1/browser/start", {"user_id": user_id})
        if payload.get("code") != 0:
            raise RuntimeError(f"Failed to start browser for {user_id}: {payload.get('msg')}")

        data = payload["data"]
        webdriver_path = data["webdriver"]
        ws_selenium = data["ws"]["selenium"]
        debugger_address = ws_selenium.replace("ws://", "").split("/devtools")[0]

        return BrowserSession(
            webdriver_path=webdriver_path,
            debugger_address=debugger_address,
            user_id=user_id,
        )

    def stop_browser(self, user_id: str) -> None:
        self.api_get_json(
            "/api/v1/browser/stop",
            {"user_id": user_id},
            retries=2,
            timeout=10,
        )

    def is_browser_active(self, user_id: str) -> bool:
        candidate_endpoints = (
            ("/api/v1/browser/active", {"user_id": user_id}),
            ("/api/v1/browser/status", {"user_id": user_id}),
        )
        last_error: Exception | None = None
        for path, params in candidate_endpoints:
            try:
                payload = self.api_get_json(
                    path,
                    params,
                    retries=1,
                    timeout=10,
                )
            except AdsPowerCapabilityError as error:
                last_error = error
                continue
            if payload.get("code") != 0:
                raise RuntimeError(
                    f"Failed to query browser active state for {user_id}: {payload.get('msg')}"
                )
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("status", "active", "is_active", "open"):
                    if key in data:
                        value = data.get(key)
                        if isinstance(value, bool):
                            return value
                        if isinstance(value, (int, float)):
                            return bool(value)
                        lowered = str(value).strip().lower()
                        return lowered in {"1", "true", "active", "opened", "open", "running"}
            if isinstance(data, bool):
                return data
            if isinstance(data, (int, float)):
                return bool(data)
            if data is not None:
                lowered = str(data).strip().lower()
                return lowered in {"1", "true", "active", "opened", "open", "running"}
        if last_error is not None:
            raise last_error
        raise AdsPowerCapabilityError("AdsPower Local API browser-active endpoint is unsupported")

    def update_profile_remark(self, user_id: str, remark: str) -> None:
        payload = self.api_post_json(
            "/api/v2/browser-profile/update",
            {"profile_id": user_id, "remark": remark},
            retries=2,
            timeout=10,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to update remark for {user_id}: {payload.get('msg')}"
            )

    @staticmethod
    def _parse_http_proxy(proxy_value: str) -> dict[str, str]:
        parts = [part.strip() for part in proxy_value.split(":", 3)]
        if len(parts) != 4 or any(not part for part in parts):
            raise ValueError(
                "BOT_TEXTING_PROXY_HTTP must be in host:port:username:password format"
            )
        host, port, username, password = parts
        return {
            "proxy_soft": "other",
            "proxy_type": "http",
            "proxy_host": host,
            "proxy_port": port,
            "proxy_user": username,
            "proxy_password": password,
        }

    def update_profile_http_proxy(self, user_id: str, proxy_value: str) -> None:
        payload = self.api_post_json(
            "/api/v1/user/update",
            {
                "user_id": user_id,
                "user_proxy_config": self._parse_http_proxy(proxy_value),
            },
            retries=2,
            timeout=15,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to update proxy for {user_id}: {payload.get('msg')}"
            )

    def delete_profiles(self, user_ids: list[str]) -> None:
        if not user_ids:
            return
        payload = self.api_post_json(
            "/api/v2/browser-profile/delete",
            {"profile_id": list(user_ids)},
            retries=2,
            timeout=20,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to delete profiles {user_ids}: {payload.get('msg')}"
            )

    def create_profile(self, profile_payload: dict[str, Any]) -> str:
        payload = self.api_post_json(
            "/api/v2/browser-profile/create",
            profile_payload,
            retries=2,
            timeout=30,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to create browser profile: {payload.get('msg')}"
            )
        data = payload.get("data") or {}
        profile_id = str(data.get("profile_id") or "").strip()
        if not profile_id:
            raise RuntimeError("AdsPower create profile response did not include profile_id")
        return profile_id

    def update_profile_saved_proxy(self, user_id: str, proxy_id: str) -> None:
        payload = self.api_post_json(
            "/api/v2/browser-profile/update",
            {"profile_id": user_id, "proxyid": proxy_id},
            retries=2,
            timeout=15,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to assign saved proxy {proxy_id!r} to {user_id}: {payload.get('msg')}"
            )

    def update_profile_extension_category(self, user_id: str, category_id: str) -> None:
        normalized_category_id = str(category_id).strip()
        if not normalized_category_id:
            raise ValueError("category_id must not be empty")

        candidate_payloads = (
            {"profile_id": user_id, "category_id": normalized_category_id},
            {"profile_id": user_id, "sys_app_cate_id": normalized_category_id},
            {"user_id": user_id, "sys_app_cate_id": normalized_category_id},
        )
        last_error: Exception | None = None

        for payload_body in candidate_payloads:
            try:
                if "profile_id" in payload_body:
                    payload = self.api_post_json(
                        "/api/v2/browser-profile/update",
                        payload_body,
                        retries=2,
                        timeout=15,
                    )
                else:
                    payload = self.api_post_json(
                        "/api/v1/user/update",
                        payload_body,
                        retries=2,
                        timeout=15,
                    )
            except Exception as error:  # noqa: BLE001
                last_error = error
                continue

            if payload.get("code") == 0:
                return

            last_error = RuntimeError(
                f"AdsPower rejected extension category update for {user_id}: {payload.get('msg')}"
            )

        if last_error is None:
            raise RuntimeError(f"Failed to update extension category for {user_id}")
        raise last_error

    def get_profile(self, user_id: str) -> dict[str, Any]:
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")

        payload = self.api_post_json(
            "/api/v2/browser-profile/list",
            {
                "profile_id": [normalized_user_id],
                "page": 1,
                "limit": 1,
            },
            retries=2,
            timeout=15,
        )
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Failed to query profile {normalized_user_id}: {payload.get('msg')}"
            )

        data = payload.get("data") or {}
        profiles = data.get("list") or []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = str(profile.get("profile_id") or "").strip()
            if profile_id == normalized_user_id:
                return profile

        raise RuntimeError(f"AdsPower query did not return profile {normalized_user_id}")

    def list_saved_proxies(self, *, page: int = 1, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
        payload = self.api_post_json(
            "/api/v2/proxy-list/list",
            {"page": page, "limit": limit},
            retries=2,
            timeout=20,
        )
        if payload.get("code") != 0:
            raise RuntimeError(f"Failed to query saved proxies: {payload.get('msg')}")
        data = payload.get("data") or {}
        return list(data.get("list") or []), int(data.get("total") or 0)

    def trigger_cookie_rpa_job(self, user_id: str, job_id: str) -> str:
        raise AdsPowerCapabilityError(
            "AdsPower cookie-build RPA trigger is not configured for this project"
        )

    def poll_cookie_rpa_job(self, job_run_id: str) -> dict[str, Any]:
        raise AdsPowerCapabilityError(
            "AdsPower cookie-build RPA status polling is not configured for this project"
        )
