# ruff: noqa: BLE001
"""复用注册登录态获取 OpenAI Platform OAuth AT/RT。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus, urlencode, urlparse

from config import chatgpt2api as _cfg
from core.openai_auth import _is_transient_network_error

logger = logging.getLogger(__name__)

_PLATFORM_OAUTH_NETWORK_RETRIES = 3
_PLATFORM_OAUTH_RETRY_DELAY_SECONDS = 3.0

PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
PLATFORM_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
PLATFORM_SCOPE = "openid profile email offline_access"
PLATFORM_AUTH0_CLIENT = (
    "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
)


@dataclass(frozen=True)
class PlatformAuthorization:
    url: str
    code_verifier: str
    state: str
    nonce: str
    device_id: str


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    code_verifier = _base64url(secrets.token_bytes(64))
    code_challenge = _base64url(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    )
    return code_verifier, code_challenge


def build_platform_authorization(email: str, device_id: str | None = None) -> PlatformAuthorization:
    code_verifier, code_challenge = generate_pkce()
    state = _base64url(secrets.token_bytes(32))
    nonce = _base64url(secrets.token_bytes(32))
    did = str(device_id or uuid.uuid4())
    params = {
        "issuer": "https://auth.openai.com",
        "client_id": PLATFORM_CLIENT_ID,
        "audience": PLATFORM_AUDIENCE,
        "redirect_uri": PLATFORM_REDIRECT_URI,
        "device_id": did,
        "screen_hint": "login",
        "max_age": "0",
        "login_hint": str(email or "").strip(),
        "scope": PLATFORM_SCOPE,
        "response_type": "code",
        "response_mode": "query",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": PLATFORM_AUTH0_CLIENT,
    }
    return PlatformAuthorization(
        url=f"{PLATFORM_AUTHORIZE_URL}?{urlencode(params)}",
        code_verifier=code_verifier,
        state=state,
        nonce=nonce,
        device_id=did,
    )


def extract_authorization_code(
    final_url: str,
    *,
    expected_state: str,
    response_body: str = "",
) -> str:
    """从 Platform callback URL 提取 code，并校验服务端返回的 state。"""
    query = parse_qs(urlparse(str(final_url or "")).query)
    error = str((query.get("error") or [""])[0] or "")
    if error:
        description = str((query.get("error_description") or [""])[0] or "")
        raise RuntimeError(f"Platform OAuth 授权失败: {error}: {description}")
    returned_state = str((query.get("state") or [""])[0] or "")
    if returned_state and returned_state != expected_state:
        raise RuntimeError("Platform OAuth state 校验失败")
    code = str((query.get("code") or [""])[0] or "").strip()
    if not code and response_body:
        match = re.search(r"[?&]code=([A-Za-z0-9._~+/%-]+)", response_body)
        if not match:
            match = re.search(r'"code"\s*:\s*"([^"]+)"', response_body)
        if match:
            code = unquote_plus(match.group(1)).strip()
    if not code:
        raise RuntimeError("Platform OAuth 未获取到 authorization code")
    return code


def _status_code(response) -> int:
    value = getattr(response, "status_code", None)
    if value is None:
        value = getattr(response, "status", 0)
    return int(value or 0)


def _response_url(response) -> str:
    value = getattr(response, "url", "")
    return str(value() if callable(value) else value or "")


def _response_text(response) -> str:
    value = getattr(response, "text", "")
    try:
        return str(value() if callable(value) else value or "")
    except json.JSONDecodeError:
        return ""


def _response_json(response) -> dict:
    value = getattr(response, "json", None)
    try:
        payload = value() if callable(value) else value
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload
    text = _response_text(response)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


def _authorize_headers(session=None) -> dict:
    headers = {}
    if session is not None and hasattr(session, "get_auth_navigate_headers"):
        headers.update(session.get_auth_navigate_headers(referer="https://chatgpt.com/"))
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "auth0-client": PLATFORM_AUTH0_CLIENT,
        "Referer": "https://chatgpt.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
    })
    return headers


def _token_headers(session=None) -> dict:
    headers = {}
    if session is not None and hasattr(session, "_get_common_headers"):
        headers.update(session._get_common_headers())
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://auth.openai.com",
        "Referer": "https://auth.openai.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    return headers


def _token_body(code: str, code_verifier: str) -> str:
    return urlencode({
        "client_id": PLATFORM_CLIENT_ID,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": PLATFORM_REDIRECT_URI,
    })


def _validate_token_response(response) -> dict:
    status = _status_code(response)
    payload = _response_json(response)
    if not 200 <= status < 300:
        message = payload.get("error_description") or payload.get("error") or _response_text(response)[:240]
        raise RuntimeError(f"Platform OAuth token 交换失败 HTTP {status}: {message}")
    if not payload.get("access_token"):
        raise RuntimeError("Platform OAuth token 响应缺少 access_token")
    return payload


def _ensure_http_oai_did(session, device_id: str) -> None:
    jar = getattr(getattr(session, "session", None), "cookies", None)
    if jar is None:
        return
    for domain in ("auth.openai.com", ".openai.com"):
        try:
            jar.set("oai-did", device_id, domain=domain, path="/")
        except Exception as exc:
            logger.debug("[Platform OAuth] HTTP 写入 oai-did Cookie 失败: %s", exc)


def get_platform_oauth_tokens(session, email: str) -> dict:
    """复用 BrowserSession/curl_cffi Cookie Jar 获取 Platform OAuth token。"""
    auth = build_platform_authorization(email, getattr(session, "device_id", None))
    _ensure_http_oai_did(session, auth.device_id)
    response = session.get(
        auth.url,
        headers=_authorize_headers(session),
        allow_redirects=True,
    )
    body = "" if "code=" in _response_url(response) else _response_text(response)
    code = extract_authorization_code(
        _response_url(response), expected_state=auth.state, response_body=body
    )
    token_response = session.post(
        PLATFORM_TOKEN_URL,
        headers=_token_headers(session),
        data=_token_body(code, auth.code_verifier),
    )
    return _validate_token_response(token_response)


def _playwright_device_id(context) -> str:
    try:
        for cookie in context.cookies():
            if cookie.get("name") == "oai-did" and cookie.get("value"):
                return str(cookie["value"])
    except Exception as exc:
        logger.debug("[Platform OAuth] Playwright 读取 oai-did Cookie 失败: %s", exc)
    return str(uuid.uuid4())


def _ensure_playwright_oai_did(context, device_id: str) -> None:
    try:
        context.add_cookies([
            {
                "name": "oai-did",
                "value": device_id,
                "domain": ".auth.openai.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "oai-did",
                "value": device_id,
                "domain": ".openai.com",
                "path": "/",
                "secure": True,
            },
        ])
    except Exception as exc:
        logger.debug("[Platform OAuth] Playwright 写入 oai-did Cookie 失败: %s", exc)


def get_platform_oauth_tokens_playwright(context, email: str) -> dict:
    """使用 Playwright BrowserContext 的 Cookie-aware HTTP client 完成 OAuth。"""
    request = getattr(context, "request", None)
    if context is None or request is None or not hasattr(request, "get"):
        raise RuntimeError("Playwright BrowserContext request 不可用")
    auth = build_platform_authorization(email, _playwright_device_id(context))
    _ensure_playwright_oai_did(context, auth.device_id)
    timeout_ms = max(1, int(getattr(_cfg, "CHATGPT2API_TIMEOUT", 30) or 30)) * 1000
    authorize_response = request.get(
        auth.url,
        headers=_authorize_headers(),
        timeout=timeout_ms,
    )
    final_url = _response_url(authorize_response)
    body = "" if "code=" in final_url else _response_text(authorize_response)
    code = extract_authorization_code(
        final_url, expected_state=auth.state, response_body=body
    )
    token_response = request.post(
        PLATFORM_TOKEN_URL,
        headers=_token_headers(),
        data=_token_body(code, auth.code_verifier),
        timeout=timeout_ms,
    )
    return _validate_token_response(token_response)


def _selenium_device_id(driver) -> str:
    for cookie in _selenium_all_cookies(driver):
        if cookie.get("name") == "oai-did" and cookie.get("value"):
            return str(cookie["value"])
    return str(uuid.uuid4())


def _selenium_all_cookies(driver) -> list[dict]:
    """读取整个 Chromium Cookie Jar，避免仅导出当前域名 Cookie。"""
    for method in ("Storage.getCookies", "Network.getAllCookies"):
        try:
            result = driver.execute_cdp_cmd(method, {}) or {}
            cookies = result.get("cookies") if isinstance(result, dict) else None
            if isinstance(cookies, list) and cookies:
                return [cookie for cookie in cookies if isinstance(cookie, dict)]
        except Exception as exc:
            logger.debug("[Platform OAuth] Selenium 读取 Cookie 失败 method=%s error=%s", method, exc)
    try:
        cookies = driver.get_cookies()
    except Exception as exc:
        logger.debug("[Platform OAuth] Selenium 回退读取 Cookie 失败: %s", exc)
        return []
    return [cookie for cookie in cookies if isinstance(cookie, dict)]


def _copy_selenium_cookies(driver, http_session) -> int:
    jar = getattr(getattr(http_session, "session", None), "cookies", None)
    if jar is None or not hasattr(jar, "set"):
        raise RuntimeError("HTTP OAuth session Cookie Jar 不可用")
    copied = 0
    for cookie in _selenium_all_cookies(driver):
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").strip()
        path = str(cookie.get("path") or "/")
        if not name or not domain:
            continue
        try:
            jar.set(name, value, domain=domain, path=path)
            copied += 1
        except Exception as exc:
            logger.debug("[Platform OAuth] 导入 Selenium Cookie 失败 name=%s error=%s", name, exc)
    return copied


def _is_missing_authorization_code_error(exc: Exception) -> bool:
    return "Platform OAuth 未获取到 authorization code" in str(exc)


def _complete_platform_authorization_in_selenium(
    driver,
    authorization: PlatformAuthorization,
    email: str,
    *,
    otp_provider=None,
    browser_helpers=None,
) -> str:
    """在同一浏览器完成 Platform OAuth 的邮箱 OTP 回退流程。

    Platform OAuth 通常可由导入 Cookie 的 HTTP 会话直接完成。服务端要求
    ``max_age=0`` 重认证时，HTTP 请求会停在邮箱验证页而没有 callback code；
    此时必须回到原 Roxy 会话处理页面，避免另开浏览器后丢失设备指纹和 Cookie。
    """
    if otp_provider is None:
        from core.email_provider import wait_for_otp
        otp_provider = wait_for_otp
    if browser_helpers is None:
        from core import roxy_registration as browser_helpers

    otp_after_ts = time.time()
    try:
        safe_get = getattr(browser_helpers, "_safe_get", None)
        if callable(safe_get):
            safe_get(
                driver,
                authorization.url,
                timeout=45,
                attempts=2,
                accept_hosts=("auth.openai.com", "platform.openai.com"),
            )
        else:
            driver.get(authorization.url)
    except Exception as exc:
        raise RuntimeError(
            f"Platform OAuth 浏览器授权页打开失败: {type(exc).__name__}: {str(exc)[:180]}"
        ) from exc

    timeout = max(30, int(getattr(_cfg, "CHATGPT2API_TIMEOUT", 30) or 30) * 3)
    deadline = time.time() + timeout
    otp_attempts = 0
    login_submitted = False
    passwordless_attempted = False

    while time.time() < deadline:
        current_url = str(getattr(driver, "current_url", "") or "")
        try:
            return extract_authorization_code(
                current_url,
                expected_state=authorization.state,
            )
        except Exception as exc:
            if not _is_missing_authorization_code_error(exc):
                raise

        if browser_helpers._is_email_verification_page(driver):
            otp_attempts += 1
            if otp_attempts > 3:
                raise RuntimeError("Platform OAuth 邮箱验证码连续错误或过期")
            logger.info(
                "[Platform OAuth] 浏览器要求邮箱验证，使用原邮箱来源接码: email=%s attempt=%s/3",
                email,
                otp_attempts,
            )
            code = otp_provider(email, after_ts=otp_after_ts)
            browser_helpers._clear_otp_inputs(driver)
            browser_helpers._type_otp(driver, code)
            try:
                browser_helpers._click_continue(driver)
            except Exception:
                # 部分 OTP 控件会在填满最后一格后自动提交。
                pass
            if browser_helpers._wait_after_email_otp_submit(driver, timeout=12) == "accepted":
                continue
            if otp_attempts >= 3:
                raise RuntimeError("Platform OAuth 邮箱验证码连续错误或过期")
            otp_after_ts = time.time()
            browser_helpers._click_resend_email_otp(driver, timeout=25)
            continue

        # login_hint 通常会直接带到 OTP 页；少数页面仍要求先填写邮箱。
        # 只尝试一次，并沿用注册流程中对第三方登录入口的保护。
        if not login_submitted and browser_helpers._is_email_login_page_still_present(driver):
            login_submitted = True
            logger.info("[Platform OAuth] 浏览器要求重新输入邮箱: email=%s", email)
            try:
                next_state = browser_helpers._submit_email_and_wait_next(driver, email, attempts=1)
            except RuntimeError:
                # 注册辅助函数在登录密码页会主动报错；这里继续检查是否有安全的
                # passwordless 入口，不能让这个预期分支中断 Platform OAuth。
                if not browser_helpers._is_login_password_page(driver):
                    raise
                next_state = "login_password"
            if next_state == "otp":
                continue
            if next_state == "logged_in":
                continue

        if browser_helpers._is_login_password_page(driver):
            if not passwordless_attempted:
                passwordless_attempted = True
                result = browser_helpers._click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    logger.info(
                        "[Platform OAuth] 登录密码页已切换到邮箱一次性验证码: email=%s",
                        email,
                    )
                    continue
            raise RuntimeError(
                "Platform OAuth 需要密码重新登录，当前自动回退仅处理邮箱验证码"
            )
        time.sleep(0.5)

    raise RuntimeError(
        "Platform OAuth 浏览器授权超时，未获取到 authorization code"
    )


def get_platform_oauth_tokens_selenium(
    driver,
    email: str,
    proxy: str | None = None,
    *,
    otp_provider=None,
) -> dict:
    """导入 Selenium 登录态后完成 OAuth；重认证时回到同一浏览器处理邮箱 OTP。"""
    from core.session import BrowserSession

    http_session = BrowserSession(proxy=proxy)
    http_session.device_id = _selenium_device_id(driver)
    copied = _copy_selenium_cookies(driver, http_session)
    _ensure_http_oai_did(http_session, http_session.device_id)
    logger.debug("[Platform OAuth] Selenium Cookie 已导入 HTTP 会话: count=%s", copied)
    authorization = build_platform_authorization(email, http_session.device_id)
    response = http_session.get(
        authorization.url,
        headers=_authorize_headers(http_session),
        allow_redirects=True,
    )
    body = "" if "code=" in _response_url(response) else _response_text(response)
    try:
        code = extract_authorization_code(
            _response_url(response),
            expected_state=authorization.state,
            response_body=body,
        )
    except Exception as exc:
        if not _is_missing_authorization_code_error(exc):
            raise
        logger.info(
            "[Platform OAuth] HTTP 授权未返回 callback code，回退同一浏览器处理重新认证: email=%s",
            email,
        )
        code = _complete_platform_authorization_in_selenium(
            driver,
            authorization,
            email,
            otp_provider=otp_provider,
        )
        # 邮箱验证会轮换或新增 auth Cookie，回写 HTTP 会话后再换 token。
        copied = _copy_selenium_cookies(driver, http_session)
        _ensure_http_oai_did(http_session, authorization.device_id)
        logger.debug("[Platform OAuth] OTP 后重新导入 Selenium Cookie: count=%s", copied)

    token_response = http_session.post(
        PLATFORM_TOKEN_URL,
        headers=_token_headers(http_session),
        data=_token_body(code, authorization.code_verifier),
    )
    return _validate_token_response(token_response)


def finalize_platform_oauth(tokens: dict, email: str) -> dict:
    """保留平台 token；存在 RT 时额外落盘完整 Codex 凭证。"""
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    if not access_token:
        raise RuntimeError("Platform OAuth 未返回 access_token")

    result = {
        "status": "success" if refresh_token else "partial",
        "ok": True,
        "has_refresh_token": bool(refresh_token),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_in": int(tokens.get("expires_in") or 0),
        "token_type": str(tokens.get("token_type") or ""),
        "file_path": None,
        "message": "已获取 Platform AT/RT" if refresh_token else "已获取 Platform AT，但未返回 RT",
    }
    if refresh_token:
        from core.codex_oauth import (
            _parse_id_token,
            build_codex_storage,
            save_codex_credential,
        )

        try:
            claims = _parse_id_token(id_token)
            claims["email"] = str(claims.get("email") or email).strip()
            storage = build_codex_storage(tokens, claims)
            path = save_codex_credential(
                storage,
                claims["email"],
                str(claims.get("plan_type") or ""),
            )
            result["file_path"] = str(path)
        except Exception as exc:
            # token 已成功拿到时不能因本地 Codex 文件写入失败而丢弃 RT；上传仍优先使用完整结构。
            result["credential_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            result["message"] += "；Codex 凭证落盘失败"
            logger.warning(
                "[Platform OAuth] RT 已获取，但 Codex 凭证落盘失败: email=%s error=%s",
                email,
                result["credential_error"],
            )
    return result


def _run(fetcher, email: str) -> dict:
    if not bool(getattr(_cfg, "ENABLE_PLATFORM_OAUTH", True)):
        return {"status": "skipped", "ok": False, "has_refresh_token": False, "message": "ENABLE_PLATFORM_OAUTH=False"}

    last_exc: Exception | None = None
    retries_used = 0
    for attempt in range(_PLATFORM_OAUTH_NETWORK_RETRIES + 1):
        try:
            result = finalize_platform_oauth(fetcher(), email)
            logger.info(
                "[Platform OAuth] 完成: email=%s refresh_token=%s file=%s",
                email,
                "yes" if result.get("has_refresh_token") else "no",
                result.get("file_path") or "-",
            )
            return result
        except Exception as exc:
            last_exc = exc
            if (
                not _is_transient_network_error(exc)
                or attempt >= _PLATFORM_OAUTH_NETWORK_RETRIES
            ):
                break

            retries_used = attempt + 1
            logger.warning(
                "[Platform OAuth] 临时性网络错误，%.1fs 后重试: "
                "email=%s retry=%s/%s error=%s",
                _PLATFORM_OAUTH_RETRY_DELAY_SECONDS,
                email,
                retries_used,
                _PLATFORM_OAUTH_NETWORK_RETRIES,
                f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            time.sleep(_PLATFORM_OAUTH_RETRY_DELAY_SECONDS)

    exc = last_exc or RuntimeError("Platform OAuth 获取失败但没有异常记录")
    logger.warning(
        "[Platform OAuth] 获取失败（保留 ChatGPT AT 并继续注册）: "
        "email=%s network_retries=%s/%s error=%s",
        email,
        retries_used,
        _PLATFORM_OAUTH_NETWORK_RETRIES,
        f"{type(exc).__name__}: {str(exc)[:200]}",
    )
    return {
        "status": "failed",
        "ok": False,
        "has_refresh_token": False,
        "message": f"{type(exc).__name__}: {str(exc)[:220]}",
    }


def run_platform_oauth_http(session, email: str) -> dict:
    return _run(lambda: get_platform_oauth_tokens(session, email), email)


def run_platform_oauth_playwright(context, email: str) -> dict:
    return _run(lambda: get_platform_oauth_tokens_playwright(context, email), email)


def run_platform_oauth_selenium(
    driver,
    email: str,
    proxy: str | None = None,
    *,
    otp_provider=None,
) -> dict:
    return _run(
        lambda: get_platform_oauth_tokens_selenium(
            driver,
            email,
            proxy=proxy,
            otp_provider=otp_provider,
        ),
        email,
    )
