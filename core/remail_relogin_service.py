# -*- coding: utf-8 -*-
"""Remail 长效邮箱账号补登服务。

长效 Remail 订单保存的是可重复取件的 serviceToken。账号失去 ChatGPT 登录态后，
本模块按账号注册时记录的驱动重新走一次邮箱登录 OTP，取得新的 ChatGPT
accessToken，并复用同一登录态获取 Platform OAuth AT/RT。任务完成后由
chatgpt2api_client 按现有配置同步完整 Codex 结构。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core import chatgpt2api_client, db, remail_client

logger = logging.getLogger(__name__)

_MAX_WORKERS = 16
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_LOG_LOCK = threading.Lock()


class _RedactingFormatter(logging.Formatter):
    """格式化补登日志，并隐藏登录凭证和一次性验证码。"""

    _PATTERNS = (
        re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*bearer\s+)[^\s,;]+"),
        re.compile(r"(?i)([?&](?:access_token|refresh_token|id_token|service_?token|serviceToken|token|code)=)[^&\s]+"),
        re.compile(
            r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|service[_-]?token|serviceToken|password)\b\s*[:=]\s*)"
            r"(?:['\"])?[^\s,;&}\]]+"
        ),
        re.compile(r"(?i)(\b(?:OTP|验证码)\b[^\r\n\d]{0,16})\d{4,8}\b"),
        re.compile(r"()\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        re.compile(r"()\b(?:rt|at|sess|sk)[_-][A-Za-z0-9._~-]{12,}\b", re.IGNORECASE),
    )

    def __init__(self, fmt: str, *, datefmt: str | None = None, secrets=()):
        super().__init__(fmt, datefmt=datefmt)
        self._secrets: set[str] = set()
        self.add_secrets(secrets)

    def add_secrets(self, secrets) -> None:
        for secret in secrets or ():
            value = str(secret or "").strip()
            if value:
                self._secrets.add(value)

    def redact(self, value: str) -> str:
        text = str(value or "")
        for pattern in self._PATTERNS:
            text = pattern.sub(r"\1[已隐藏]", text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[已隐藏]")
        return text

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


def _sanitize_log_text(value: str, secrets=()) -> str:
    return _RedactingFormatter("%(message)s", secrets=secrets).redact(str(value or ""))


def log_path(email: str) -> Path:
    """返回某个长效邮箱账号的补登日志路径。"""
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"remail-relogin-{safe}.log"


def _append_log(
    email: str,
    message: str,
    *,
    level: str = "INFO",
    clear: bool = False,
    secrets=(),
) -> bool:
    """写入不含凭证的补登里程碑；详细线程日志由 FileHandler 追加。"""
    try:
        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        text = _sanitize_log_text(str(message or "").rstrip(), secrets=secrets)
        with _LOG_LOCK, path.open("w" if clear else "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{str(level or 'INFO').upper()}] {text}\n")
        return True
    except Exception as exc:
        logger.warning(
            "[Remail补登] 写入账号日志失败: email=%s error=%s",
            email,
            f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return False


def prepare_log(email: str, *, batch_id: str, workers: int, index: int, total: int) -> None:
    _append_log(
        email,
        f"[Remail补登] 已加入队列 batch={batch_id} account={index}/{total} workers={workers}",
        clear=True,
    )


def _attach_thread_log(
    email: str,
    *,
    secrets=(),
) -> tuple[logging.Logger, logging.FileHandler, _RedactingFormatter] | None:
    """捕获当前补登线程以及其同步调用链产生的详细日志。"""
    try:
        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        thread_name = threading.current_thread().name
        handler = logging.FileHandler(str(path), mode="a", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        formatter = _RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
            secrets=secrets,
        )
        handler.setFormatter(formatter)
        handler.addFilter(lambda record: record.threadName == thread_name)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        return root_logger, handler, formatter
    except Exception as exc:
        logger.warning(
            "[Remail补登] 初始化详细日志失败: email=%s error=%s",
            email,
            f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None


def _otp_provider_default():
    from core.email_provider import wait_for_otp

    return wait_for_otp


def _valid_proxy(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    return raw if parsed.scheme in {"http", "https", "socks5", "socks5h"} and parsed.netloc else None


def _wait_protocol_otp(session, email: str, after_ts: float, otp_provider, *, resend):
    """等待协议登录 OTP；无效/超时时由调用方重新触发邮件。"""
    from core.openai_auth import EmailOtpInvalidError, send_email_otp, validate_email_otp

    current = None
    for attempt in range(1, 4):
        if current is None:
            current = otp_provider(email, after_ts=after_ts)
        try:
            result = validate_email_otp(session, current, None, None)
            return result
        except EmailOtpInvalidError:
            if attempt >= 3:
                raise
            after_ts = time.time()
            if resend is not None:
                resend()
            else:
                send_email_otp(session)
            current = None
    raise RuntimeError("邮箱验证码连续失败")


def run_protocol_relogin(
    email: str,
    *,
    proxy: str | None = None,
    otp_provider=None,
) -> dict:
    """使用原 protocol 驱动重新登录已有 ChatGPT 账号。"""
    from config import email as email_cfg
    from config import openai_protocol as protocol_cfg
    from core.chatgpt_auth import get_csrf_token, get_providers, signin_openai
    from core.chatgpt_bootstrap import authenticated_bootstrap, anonymous_bootstrap
    from core.openai_auth import follow_authorize, network_preflight
    from core.session import BrowserSession
    from main import _finalize_registration_session

    otp_provider = otp_provider or _otp_provider_default()
    session = BrowserSession(proxy=proxy)
    network_preflight(session)
    if getattr(protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", True):
        anonymous_bootstrap(
            session,
            strict=bool(getattr(protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
        )
    csrf_token = get_csrf_token(session)
    authorize_url = signin_openai(session, csrf_token, email)
    otp_after_ts = time.time()
    follow_authorize(session, authorize_url)
    validate_result = _wait_protocol_otp(
        session,
        email,
        otp_after_ts,
        otp_provider,
        resend=None,
    )

    page = validate_result.get("page") if isinstance(validate_result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    continue_url = (
        validate_result.get("continue_url")
        or validate_result.get("external_url")
        or validate_result.get("url")
        or page.get("continue_url")
        or page.get("external_url")
        or page.get("url")
    )
    continue_text = str(continue_url or "")
    direct_callback = bool(
        continue_text
        and "about-you" not in continue_text
        and (
            "chatgpt.com/api/auth/callback" in continue_text
            or "auth.openai.com/authorize/continue" in continue_text
            or page_type == "external_url"
        )
    )
    if not direct_callback:
        raise RuntimeError(
            f"Remail 补登 OTP 后未进入已有账号 OAuth 回调: page_type={page_type or '-'}"
        )
    session_info, access_token = _finalize_registration_session(
        session,
        continue_url,
        email,
        callback_referer="https://auth.openai.com/email-verification",
    )
    if getattr(protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True):
        authenticated_bootstrap(
            session,
            access_token,
            strict=bool(getattr(protocol_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
        )
    from core.platform_oauth import run_platform_oauth_http

    platform_oauth = run_platform_oauth_http(session, email)
    return {
        "access_token": access_token,
        "session_info": session_info,
        "platform_oauth": platform_oauth,
        "driver": "protocol",
    }


def _selenium_fill_password(driver, password: str) -> bool:
    """填写已有账号登录密码；返回是否找到并提交密码页。"""
    if not password:
        return False
    try:
        target = driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i]')].find(visible);
            if (!input) return null;
            const form = input.closest('form');
            const button = [...(form || document).querySelectorAll('button[type="submit"],input[type="submit"],button')]
              .find(el => visible(el));
            return {input, button};
            """
        )
        if not isinstance(target, dict) or not target.get("input"):
            return False
        from core.roxy_registration import _human_click, _human_type_text

        _human_type_text(driver, target["input"], password, clear=True)
        if target.get("button"):
            _human_click(driver, target["button"], label="relogin_password_submit")
        else:
            target["input"].send_keys("\ue007")  # Selenium Enter
        return True
    except Exception as exc:
        logger.info("[Remail补登] 登录密码页处理失败，将尝试一次性验证码: %s", str(exc)[:180])
        return False


def _run_selenium_relogin(
    email: str,
    *,
    driver_kind: str,
    password: str = "",
    proxy: str | None = None,
    otp_provider=None,
) -> dict:
    """Roxy/Cloak 共用 Selenium 风格登录流程。"""
    from core.roxy_registration import (
        _check_manual_stop,
        _clear_otp_inputs,
        _click_continue,
        _click_passwordless_signup_if_present,
        _fetch_chatgpt_session,
        _has_access_token,
        _is_email_verification_page,
        _maybe_accept,
        _submit_email_and_wait_next,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    otp_provider = otp_provider or _otp_provider_default()
    opened = None
    client = None
    driver = None
    keep_open = False
    if driver_kind == "roxy":
        from config import roxybrowser as driver_cfg
        from core.roxy_registration import _build_driver
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient()
        opened = client.open_profile()
        driver = _build_driver(opened)
        keep_open = bool(getattr(driver_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
    else:
        from config import cloakbrowser as driver_cfg
        from core.cloakbrowser_driver import build_cloak_driver

        driver, opened = build_cloak_driver(proxy=proxy)
        keep_open = bool(getattr(driver_cfg, "CLOAK_KEEP_BROWSER_OPEN", False))

    try:
        _check_manual_stop()
        driver.get("https://chatgpt.com/auth/login")
        _maybe_accept(driver)
        # OpenAI 可能在点击提交邮箱后立刻投递验证码；时间戳必须在提交前记录，
        # 否则较快到达的首封邮件会被 after_ts 过滤掉。
        otp_after_ts = time.time()
        try:
            next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        except RuntimeError as exc:
            if "登录密码页" not in str(exc):
                raise
            next_state = "login_password"

        if next_state in {"password", "login_password"}:
            # 补登的核心是复用 Remail 长效收件能力：优先切到一次性验证码，
            # 页面没有该入口时才回退到注册时保存的密码。
            otp_after_ts = time.time()
            passwordless = _click_passwordless_signup_if_present(driver)
            passwordless_ok = isinstance(passwordless, dict) and bool(passwordless.get("ok"))
            filled = False if passwordless_ok else (_selenium_fill_password(driver, password) if password else False)
            if not passwordless_ok and not filled:
                raise RuntimeError("已有账号进入登录密码页，且未找到一次性验证码入口或可用保存密码")
            wait_end = time.time() + 25
            while time.time() < wait_end:
                if _is_email_verification_page(driver) or _has_access_token(driver):
                    break
                time.sleep(0.5)

        if not _has_access_token(driver):
            current_otp = None
            for attempt in range(1, 4):
                if current_otp is None:
                    current_otp = otp_provider(email, after_ts=otp_after_ts)
                _clear_otp_inputs(driver)
                _type_otp(driver, current_otp)
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                if _wait_after_email_otp_submit(driver, timeout=12) == "accepted":
                    break
                if attempt >= 3:
                    raise RuntimeError("Remail 补登邮箱验证码连续错误/过期")
                otp_after_ts = time.time()
                from core.roxy_registration import _click_resend_email_otp

                _click_resend_email_otp(driver, timeout=25)
                current_otp = None

        session_info = _fetch_chatgpt_session(driver, timeout=120, auto_jump_wait=10)
        access_token = str(session_info.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("Remail 补登完成但没有 ChatGPT accessToken")
        from core.platform_oauth import run_platform_oauth_selenium

        platform_oauth = run_platform_oauth_selenium(
            driver,
            email,
            proxy=proxy,
            otp_provider=otp_provider,
        )
        return {
            "access_token": access_token,
            "session_info": session_info,
            "platform_oauth": platform_oauth,
            "driver": driver_kind,
        }
    finally:
        if not keep_open:
            try:
                if driver is not None:
                    driver.quit()
            except Exception:
                pass
            if client is not None and opened is not None:
                try:
                    client.cleanup_profile(opened)
                except Exception:
                    pass


def _browser_use_fill_password(page, password: str) -> bool:
    if not password:
        return False
    from core.browser_use_registration import _click_first, _fill_first

    ok = _fill_first(
        page,
        [
            "input[type='password']",
            "input[name='password']",
            "input[autocomplete='current-password']",
        ],
        password,
        timeout_ms=8000,
    )
    if not ok:
        return False
    if not _click_first(
        page,
        ["button[type='submit']", "form button", "button:has-text('Continue')", "button:has-text('继续')"],
        timeout_ms=8000,
    ):
        page.keyboard.press("Enter")
    return True


def _run_browser_use_relogin(
    email: str,
    *,
    password: str = "",
    proxy: str | None = None,
    otp_provider=None,
    cloud_provider: str = "browser_use",
) -> dict:
    """Browser Use/Skyvern 云端浏览器登录流程。"""
    from core.browser_use_registration import (
        _browser_use_heartbeat,
        _check_manual_stop,
        _clear_otp_inputs,
        _click_continue,
        _click_passwordless_signup_if_present,
        _click_resend_otp,
        _fetch_chatgpt_session,
        _is_email_verification_page,
        _page_url,
        _quick_auth_state,
        _submit_email_until_transition,
        _type_otp,
        _wait_after_otp,
    )

    otp_provider = otp_provider or _otp_provider_default()
    provider = str(cloud_provider or "browser_use").strip().lower()
    if provider in {"skyvern", "sv"}:
        from core.skyvern_client import SkyvernClient

        client = SkyvernClient()
        provider_prefix = "skyvern"
    else:
        from core.browser_use_client import BrowserUseClient

        client = BrowserUseClient()
        provider_prefix = "browser_use"
    session_info_open = client.open_session()
    browser = None
    context = None
    page = None
    keep_open = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            connect_kwargs = {}
            if provider_prefix == "skyvern" and hasattr(client, "cdp_headers"):
                connect_kwargs["headers"] = client.cdp_headers()
            browser = p.chromium.connect_over_cdp(session_info_open.connect_url, **connect_kwargs)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
            _check_manual_stop()
            # 与 Selenium/protocol 路径一致，在提交邮箱之前开始计时，避免漏掉首封 OTP。
            otp_after_ts = time.time()
            try:
                state = _submit_email_until_transition(page, context, email, attempts=3)
            except RuntimeError as exc:
                if "登录密码页" not in str(exc):
                    raise
                state = "login_password"

            if state in {"password", "login_password"}:
                otp_after_ts = time.time()
                passwordless_ok = bool(_click_passwordless_signup_if_present(page))
                filled = False if passwordless_ok else (_browser_use_fill_password(page, password) if password else False)
                if not passwordless_ok and not filled:
                    raise RuntimeError("已有账号进入登录密码页，且未找到一次性验证码入口或可用保存密码")
                end = time.time() + 25
                while time.time() < end:
                    page = _browser_use_heartbeat(page, context=context, label="relogin-password")
                    state_after = str((_quick_auth_state(page) or {}).get("state") or "")
                    if state_after in {"email_verification", "chatgpt"} or _is_email_verification_page(page):
                        break
                    time.sleep(0.3)

            if str((_quick_auth_state(page) or {}).get("state") or "") != "chatgpt":
                current_otp = None
                for attempt in range(1, 4):
                    if current_otp is None:
                        current_otp = otp_provider(email, after_ts=otp_after_ts)
                    _clear_otp_inputs(page)
                    _type_otp(page, current_otp)
                    try:
                        _click_continue(page)
                    except Exception:
                        pass
                    outcome = _wait_after_otp(page, timeout=12)
                    if outcome in {"accepted", "unknown"}:
                        break
                    if attempt >= 3:
                        raise RuntimeError("Remail 补登邮箱验证码连续错误/过期")
                    otp_after_ts = time.time()
                    if not _click_resend_otp(page):
                        raise RuntimeError("Remail 补登验证码失败且找不到重发入口")
                    current_otp = None

            session_info = _fetch_chatgpt_session(page, context=context, timeout=120)
            access_token = str(session_info.get("accessToken") or "").strip()
            if not access_token:
                raise RuntimeError("Remail 补登完成但没有 ChatGPT accessToken")
            from core.platform_oauth import run_platform_oauth_playwright

            platform_oauth = run_platform_oauth_playwright(context, email)
            return {
                "access_token": access_token,
                "session_info": session_info,
                "platform_oauth": platform_oauth,
                "driver": provider_prefix,
            }
    finally:
        try:
            from config import browser_use as browser_cfg

            keep_open = bool(getattr(browser_cfg, "BROWSER_USE_KEEP_BROWSER_OPEN", False))
        except Exception:
            keep_open = False
        if provider_prefix == "skyvern":
            try:
                from config import skyvern as skyvern_cfg

                keep_open = bool(getattr(skyvern_cfg, "SKYVERN_KEEP_BROWSER_OPEN", False))
            except Exception:
                keep_open = False
        if not keep_open:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            if provider_prefix == "skyvern" and hasattr(client, "close_browser_session") and getattr(session_info_open, "session_id", ""):
                try:
                    client.close_browser_session(session_info_open.session_id)
                except Exception:
                    pass


def run_relogin(
    email: str,
    *,
    driver: str = "protocol",
    password: str = "",
    proxy: str | None = None,
    otp_provider=None,
) -> dict:
    """按注册时驱动执行一次长效账号补登。"""
    mode = str(driver or "protocol").strip().lower()
    if mode in {"api", "http"}:
        mode = "protocol"
    if mode in {"roxybrowser", "fingerprint", "browser"}:
        mode = "roxy"
    if mode in {"cloakbrowser"}:
        mode = "cloak"
    if mode in {"browseruse", "browser-use", "bu"}:
        mode = "browser_use"
    if mode in {"skyvern", "sv"}:
        mode = "skyvern"
    if mode == "protocol":
        result = run_protocol_relogin(email, proxy=proxy, otp_provider=otp_provider)
    elif mode in {"roxy", "cloak"}:
        result = _run_selenium_relogin(
            email,
            driver_kind=mode,
            password=password,
            proxy=proxy,
            otp_provider=otp_provider,
        )
    elif mode in {"browser_use", "skyvern"}:
        result = _run_browser_use_relogin(
            email,
            password=password,
            proxy=proxy,
            otp_provider=otp_provider,
            cloud_provider=mode,
        )
    else:
        raise RuntimeError(f"不支持的 Remail 补登驱动: {driver}")

    access_token = str(result.get("access_token") or "").strip()
    oauth = result.get("platform_oauth") if isinstance(result.get("platform_oauth"), dict) else {}
    has_rt = bool(str(oauth.get("refresh_token") or "").strip()) or bool(oauth.get("has_refresh_token"))
    if not access_token:
        raise RuntimeError("补登驱动未返回 ChatGPT accessToken")
    result["status"] = "success" if has_rt else "partial"
    result["message"] = "补登成功，已获取 ChatGPT AT/Platform OAuth RT" if has_rt else "已获取 ChatGPT AT，但未返回 Platform OAuth RT"
    result["driver"] = mode
    return result


def _run_one(item: dict, batch_id: str, index: int, total: int) -> dict:
    acc_id = int(item["id"])
    email = str(item.get("email") or "").strip()
    log_handler = None
    log_secrets: list[str] = []
    try:
        if not db.mark_account_remail_relogin_running(acc_id):
            message = "账号已删除或补登状态已被重置"
            _append_log(email, f"[Remail补登] 跳过：{message}", level="WARNING")
            return {"ok": False, "status": "skipped", "id": acc_id, "email": email, "error": message}
        _append_log(email, f"[Remail补登] 开始执行 batch={batch_id} account={index}/{total}")
        try:
            account = db.get_account(acc_id)
            if not account or not account.get("remail_long_lived"):
                raise RuntimeError("账号缺少可用的 Remail 长效 serviceToken")
            extra = account.get("extra_json")
            if isinstance(extra, str):
                import json

                extra = json.loads(extra) if extra else {}
            extra = extra if isinstance(extra, dict) else {}
            driver = str(
                extra.get("registration_driver")
                or extra.get("relogin_driver")
                or account.get("remail_relogin_driver")
                or "protocol"
            ).strip().lower()
            password = str(account.get("registration_password") or "")
            proxy = _valid_proxy(str(account.get("proxy_used") or ""))
            stored_remail = extra.get("remail") if isinstance(extra.get("remail"), dict) else {}
            log_secrets.extend([
                password,
                str(stored_remail.get("service_token") or stored_remail.get("serviceToken") or ""),
            ])
            # 优先从持久化文件恢复；文件缺失时 get_account_context 会从账号 extra_json 迁移。
            context = remail_client.get_account_context(email)
            if context is None or not context.is_long_lived:
                raise RuntimeError("Remail 长效 serviceToken 不存在；请确认 Remail 仍允许该邮箱继续取件")
            log_secrets.append(str(context.service_token or ""))
            log_handler = _attach_thread_log(email, secrets=log_secrets)
            logger.info("[Remail补登] batch=%s %s/%s email=%s", batch_id, index, total, email)
            _append_log(
                email,
                f"[Remail补登] 已恢复长效邮箱上下文，注册驱动={driver or 'protocol'}，网络={'保存的代理' if proxy else '驱动默认网络'}",
                secrets=log_secrets,
            )
            _append_log(email, "[Remail补登] 阶段：重新登录 → 收取邮箱 OTP → 获取 ChatGPT AT → 获取 Platform OAuth AT/RT")
            result = run_relogin(email, driver=driver, password=password, proxy=proxy)
            oauth = result.get("platform_oauth") if isinstance(result.get("platform_oauth"), dict) else {}
            result_secrets = [
                str(result.get("access_token") or ""),
                str(oauth.get("access_token") or ""),
                str(oauth.get("refresh_token") or ""),
                str(oauth.get("id_token") or ""),
            ]
            log_secrets.extend(result_secrets)
            if log_handler is not None:
                log_handler[2].add_secrets(result_secrets)
            credential = {
                "status": "success" if oauth.get("file_path") else ("failed" if oauth.get("has_refresh_token") else ""),
                "message": "Codex 凭证已更新" if oauth.get("file_path") else str(oauth.get("credential_error") or ""),
            }
            _append_log(email, "[Remail补登] 登录凭证获取完成，正在执行 Codex 凭证保存与 chatgpt2api 自动上传")
            upload = chatgpt2api_client.auto_upload_registered_account(
                str(result.get("access_token") or ""),
                platform_oauth=oauth,
                email=email,
                password=password,
            )
            if not isinstance(upload, dict):
                upload = {"status": "failed", "error": "chatgpt2api 返回了无效结果"}
            result["credential"] = credential
            result["upload"] = upload
            db.complete_account_remail_relogin(acc_id, result)
            logger.info(
                "[Remail补登] 完成 email=%s status=%s credential=%s upload=%s",
                email,
                result.get("status"),
                credential.get("status") or "-",
                upload.get("status") or "-",
            )
            _append_log(
                email,
                f"[Remail补登] 完成：status={result.get('status') or '-'} Codex={credential.get('status') or '-'} upload={upload.get('status') or '-'}",
                secrets=log_secrets,
            )
            return {"id": acc_id, "email": email, "ok": True, "status": result.get("status"), "upload_status": upload.get("status")}
        except Exception as exc:
            error = _sanitize_log_text(
                f"{type(exc).__name__}: {str(exc)[:320]}",
                secrets=log_secrets,
            )
            db.complete_account_remail_relogin(
                acc_id,
                {"status": "failed", "ok": False, "error": error, "message": "长效邮箱补登失败", "driver": ""},
            )
            logger.warning("[Remail补登] 失败 email=%s error=%s", email, error)
            _append_log(email, f"[Remail补登] 失败：{error}", level="ERROR", secrets=log_secrets)
            return {"id": acc_id, "email": email, "ok": False, "status": "failed", "error": error}
    finally:
        if log_handler is not None:
            root_logger, handler, _formatter = log_handler
            root_logger.removeHandler(handler)
            handler.close()


def _run_batch(items: list[dict], workers: int, batch_id: str) -> None:
    logger.info("[Remail补登] 启动 batch=%s count=%s workers=%s", batch_id, len(items), workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"remail-relogin-{batch_id}") as executor:
        futures = [
            executor.submit(_run_one, item, batch_id, index, len(items))
            for index, item in enumerate(items, 1)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("[Remail补登] 批量子任务异常: batch=%s", batch_id)
    logger.info("[Remail补登] 批量任务完成: batch=%s", batch_id)


def start_batch(items: list[dict], workers: int = 1) -> dict:
    """异步启动已经 claim 的长效邮箱账号。"""
    if not items:
        raise ValueError("Remail 补登任务为空")
    workers = max(1, min(_MAX_WORKERS, int(workers)))
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for index, item in enumerate(items, 1):
        prepare_log(
            str(item.get("email") or "").strip(),
            batch_id=batch_id,
            workers=workers,
            index=index,
            total=len(items),
        )
    threading.Thread(
        target=_run_batch,
        args=(list(items), workers, batch_id),
        name=f"remail-relogin-dispatch-{batch_id}",
        daemon=True,
    ).start()
    return {"batch_id": batch_id, "workers": workers, "count": len(items)}
