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

logger = logging.getLogger(__name__)

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
    """复用 Playwright BrowserContext 的真实浏览器页面、Cookie 与出口代理。"""
    if context is None or not hasattr(context, "new_page"):
        raise RuntimeError("Playwright BrowserContext 不可用")
    auth = build_platform_authorization(email, _playwright_device_id(context))
    _ensure_playwright_oai_did(context, auth.device_id)
    timeout_ms = max(1, int(getattr(_cfg, "CHATGPT2API_TIMEOUT", 30) or 30)) * 1000
    page = context.new_page()
    try:
        page.goto(auth.url, wait_until="domcontentloaded", timeout=timeout_ms)
        final_url = str(getattr(page, "url", "") or "")
        body = ""
        if "code=" not in final_url:
            try:
                body = str(page.content() or "")
            except Exception:
                body = ""
        code = extract_authorization_code(
            final_url, expected_state=auth.state, response_body=body
        )
        encoded = _token_body(code, auth.code_verifier)
        try:
            raw = page.evaluate(
                """async ({url, body}) => {
                  try {
                    const response = await fetch(url, {
                      method: 'POST', credentials: 'include',
                      headers: {'accept': 'application/json', 'content-type': 'application/x-www-form-urlencoded'},
                      body
                    });
                    return {status: response.status, text: await response.text()};
                  } catch (error) {
                    return {status: 0, error: String(error)};
                  }
                }""",
                {"url": PLATFORM_TOKEN_URL, "body": encoded},
            )
            if not isinstance(raw, dict) or raw.get("error"):
                raise RuntimeError(
                    str(raw.get("error") if isinstance(raw, dict) else raw)
                )
            token_response = type("PlaywrightOAuthResponse", (), {
                "status_code": int(raw.get("status") or 0),
                "text": str(raw.get("text") or ""),
            })()
            return _validate_token_response(token_response)
        except Exception as browser_exc:
            logger.warning(
                "[Platform OAuth] 浏览器内 token 交换失败，改用 context.request: %s",
                str(browser_exc)[:180],
            )
            if getattr(context, "request", None) is None:
                raise
            token_response = context.request.post(
                PLATFORM_TOKEN_URL,
                headers=_token_headers(),
                data=encoded,
                timeout=timeout_ms,
            )
            return _validate_token_response(token_response)
    finally:
        try:
            page.close()
        except Exception as exc:
            logger.debug("[Platform OAuth] 关闭临时 Playwright 页面失败: %s", exc)


def _selenium_device_id(driver) -> str:
    try:
        for cookie in driver.get_cookies():
            if cookie.get("name") == "oai-did" and cookie.get("value"):
                return str(cookie["value"])
    except Exception as exc:
        logger.debug("[Platform OAuth] Selenium 读取 oai-did Cookie 失败: %s", exc)
    return str(uuid.uuid4())


def _ensure_selenium_oai_did(driver, device_id: str) -> None:
    try:
        for domain in (".auth.openai.com", ".openai.com"):
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": "oai-did",
                "value": device_id,
                "domain": domain,
                "path": "/",
                "secure": True,
            })
    except Exception as exc:
        logger.debug("[Platform OAuth] Selenium 写入 oai-did Cookie 失败: %s", exc)


def _open_selenium_tab(driver) -> tuple[str | None, str | None]:
    original = getattr(driver, "current_window_handle", None)
    before = set(getattr(driver, "window_handles", []) or [])
    try:
        driver.switch_to.new_window("tab")
    except Exception:
        driver.execute_script("window.open('about:blank', '_blank')")
        end = time.time() + 5
        while time.time() < end:
            handles = list(getattr(driver, "window_handles", []) or [])
            added = [item for item in handles if item not in before]
            if added:
                driver.switch_to.window(added[-1])
                break
            time.sleep(0.1)
    current = getattr(driver, "current_window_handle", None)
    return original, current


def _selenium_exchange(driver, code: str, code_verifier: str) -> dict:
    script = r"""
    const url = arguments[0], body = arguments[1], done = arguments[arguments.length - 1];
    fetch(url, {
      method: 'POST', credentials: 'include',
      headers: {'accept': 'application/json', 'content-type': 'application/x-www-form-urlencoded'},
      body
    }).then(async response => done({status: response.status, text: await response.text()}))
      .catch(error => done({status: 0, error: String(error)}));
    """
    raw = driver.execute_async_script(
        script, PLATFORM_TOKEN_URL, _token_body(code, code_verifier)
    )
    if not isinstance(raw, dict) or raw.get("error"):
        raise RuntimeError(f"浏览器内 token 交换失败: {(raw or {}).get('error', '无响应') if isinstance(raw, dict) else raw}")
    response = type("SeleniumOAuthResponse", (), {
        "status_code": int(raw.get("status") or 0),
        "text": str(raw.get("text") or ""),
    })()
    return _validate_token_response(response)


def get_platform_oauth_tokens_selenium(driver, email: str, proxy: str | None = None) -> dict:
    """在 Roxy Selenium 的同一浏览器/代理中完成授权和 token 交换。"""
    auth = build_platform_authorization(email, _selenium_device_id(driver))
    _ensure_selenium_oai_did(driver, auth.device_id)
    original = new_handle = None
    try:
        original, new_handle = _open_selenium_tab(driver)
        driver.get(auth.url)
        final_url = str(getattr(driver, "current_url", "") or "")
        body = ""
        if "code=" not in final_url:
            body = str(getattr(driver, "page_source", "") or "")
        code = extract_authorization_code(
            final_url, expected_state=auth.state, response_body=body
        )
        try:
            return _selenium_exchange(driver, code, auth.code_verifier)
        except Exception as browser_exc:
            logger.warning(
                "[Platform OAuth] 浏览器内 token 交换失败，改用 HTTP 交换: %s",
                str(browser_exc)[:180],
            )
            from core.session import BrowserSession

            fallback = BrowserSession(proxy=proxy)
            fallback.device_id = auth.device_id
            response = fallback.post(
                PLATFORM_TOKEN_URL,
                headers=_token_headers(fallback),
                data=_token_body(code, auth.code_verifier),
            )
            return _validate_token_response(response)
    finally:
        if new_handle and new_handle != original:
            try:
                driver.close()
            except Exception as exc:
                logger.debug("[Platform OAuth] 关闭临时 Selenium 标签失败: %s", exc)
        if original:
            try:
                driver.switch_to.window(original)
            except Exception as exc:
                logger.debug("[Platform OAuth] 恢复 Selenium 原标签失败: %s", exc)


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
        logger.warning(
            "[Platform OAuth] 获取失败（保留 ChatGPT AT 并继续注册）: email=%s error=%s",
            email,
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


def run_platform_oauth_selenium(driver, email: str, proxy: str | None = None) -> dict:
    return _run(lambda: get_platform_oauth_tokens_selenium(driver, email, proxy=proxy), email)
