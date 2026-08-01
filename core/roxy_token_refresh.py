# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 使用已保存密码重新登录 ChatGPT 并取得 accessToken。"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urljoin, urlparse

from config import roxybrowser as _cfg
from core.email_provider import wait_for_otp
from core.humanize import delay as human_delay
from core.roxy_registration import (
    _build_driver,
    _center_browser_window,
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _fetch_chatgpt_session,
    _has_access_token,
    _is_email_verification_page,
    _is_login_password_page,
    _maybe_accept,
    _submit_email_step,
    _type_email_address,
    _type_otp,
    _wait_after_email_otp_submit,
    _wait_email_submit_next_state,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)

# Roxy 的本地控制面无法同时处理多个 create/open/close/delete 请求，但已经
# 打开的浏览器可以并行执行页面自动化。该锁仅属于 Token 更新流程，不参与注册。
_TOKEN_REFRESH_ROXY_CONTROL_LOCK = threading.Lock()


def _is_roxy_control_busy_error(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "正在创建中",
        "请稍等",
        "creation in progress",
        "already creating",
        "browser is being created",
    ))


def _is_closed_window_error(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "no such window",
        "target window already closed",
        "web view not found",
        "当前 cloakbrowser 页面已关闭",
    ))


def _open_token_refresh_browser(client, max_attempts: int = 4):
    """串行执行 Roxy 控制面操作，返回独立的 (profile, driver)。"""
    last_exc: Exception | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        opened = None
        with _TOKEN_REFRESH_ROXY_CONTROL_LOCK:
            try:
                opened = client.open_profile()
                driver = _build_driver(opened)
                _center_browser_window(driver)
                driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
                return opened, driver
            except Exception as exc:
                last_exc = exc
                if opened is not None:
                    try:
                        client.cleanup_profile(opened)
                    except Exception:
                        pass
        if not _is_roxy_control_busy_error(last_exc) or attempt >= max_attempts:
            raise last_exc
        delay = min(6.0, 1.5 * attempt)
        logger.warning(
            "[Token刷新] Roxy 正在处理另一个环境，%.1fs 后重试创建（%s/%s）",
            delay,
            attempt + 1,
            max_attempts,
        )
        time.sleep(delay)
    raise last_exc or RuntimeError("Roxy Token 更新环境创建失败")


def _cleanup_token_refresh_browser(client, opened, driver) -> None:
    """清理也经过同一控制面锁，避免关闭环境与另一个 create/open 交叉。"""
    with _TOKEN_REFRESH_ROXY_CONTROL_LOCK:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None:
            client.cleanup_profile(opened)


def _activate_surviving_window(driver) -> bool:
    """认证回调关闭当前标签页时，切换到仍存活的标签页。"""
    try:
        handles = list(driver.window_handles or [])
    except Exception:
        handles = []
    for handle in reversed(handles):
        try:
            driver.switch_to.window(handle)
            _ = driver.current_url
            logger.info("[Token刷新] 当前认证标签页已关闭，已切换到存活标签页")
            return True
        except Exception:
            continue
    try:
        driver.switch_to.new_window("tab")
        _ = driver.current_url
        logger.info("[Token刷新] 当前认证标签页已关闭，已新建标签页继续登录")
        return True
    except Exception:
        return False


def _auth_page_state(driver) -> dict:
    try:
        return driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const text = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
            const errors = [...document.querySelectorAll('[role=alert],[aria-live=assertive],[class*=error i]')]
              .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean).slice(0, 8);
            return {url: location.href, title: document.title, text: text.slice(0, 800), errors};
            """
        ) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_transient_auth_error(state: dict) -> bool:
    url = str(state.get("url") or "").lower()
    content = " ".join((
        str(state.get("title") or ""),
        str(state.get("text") or ""),
        " ".join(str(item) for item in (state.get("errors") or [])),
    )).lower()
    if "auth.openai.com" not in url:
        return False
    return (
        "currently unable to handle this request" in content
        or "http error 500" in content
        or "http error 502" in content
        or "http error 503" in content
        or "http error 504" in content
    )


def _fill_login_password(driver, password: str) -> None:
    """填写登录密码页，不接受注册密码页，避免误把异常账号重新注册。"""
    human_delay("form")
    result = driver.execute_script(
        r"""
        const password = String(arguments[0] || '');
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="password"],input[autocomplete="current-password"]')]
          .filter(visible);
        const input = inputs[0];
        if (!input) return {ok:false, reason:'missing_login_password_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center'});
        input.focus();
        if (setter) setter.call(input, password); else input.value = password;
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button[type="submit"],input[type="submit"],button')]
          .filter(el => visible(el) && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10,
              dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_login_password_submit'};
        buttons[0].el.scrollIntoView({block:'center'});
        buttons[0].el.click();
        return {ok:true};
        """,
        password,
    ) or {}
    if not result.get("ok"):
        raise RuntimeError(f"登录密码页处理失败: {result}")
    human_delay("navigate")


def _wait_after_password_submit(driver, timeout: int = 35) -> str:
    last_state = {}
    end = time.time() + timeout
    while time.time() < end:
        if _has_access_token(driver):
            return "logged_in"
        if _is_email_verification_page(driver):
            return "otp"
        if not _is_login_password_page(driver):
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            if "chatgpt.com" in current:
                return "logged_in"
            last_state = _auth_page_state(driver)
            if _is_transient_auth_error(last_state):
                logger.warning("[Token刷新] 密码提交后认证站点返回临时错误页：%s", last_state)
                return "transient_error"
        time.sleep(0.7)
    if _is_login_password_page(driver):
        raise RuntimeError("密码登录未通过，仍停留在登录密码页")
    if not last_state:
        last_state = _auth_page_state(driver)
    raise RuntimeError(f"提交登录密码后未进入已登录或邮箱验证码页面: {last_state}")


def _recover_from_transient_auth_error(driver, timeout: int = 15) -> None:
    """认证站点明确返回 5xx 时回到密码表单，仅供一次重试。"""
    try:
        driver.back()
    except Exception:
        pass
    human_delay("navigate")
    end = time.time() + timeout
    while time.time() < end:
        if _is_login_password_page(driver):
            return
        time.sleep(0.5)

    driver.get("https://auth.openai.com/log-in/password")
    human_delay("navigate")
    end = time.time() + timeout
    while time.time() < end:
        if _is_login_password_page(driver):
            return
        time.sleep(0.5)
    raise RuntimeError(f"认证站点临时错误后无法恢复密码页: {_auth_page_state(driver)}")


def _switch_email_verification_to_password(driver, timeout: int = 25) -> str:
    """首次进入邮箱验证码页时切换到密码登录，不消费 passwordless OTP。"""
    result = driver.execute_script(
        r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = value => String(value || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')]
          .filter(el => visible(el) && enabled(el))
          .map((el, index) => {
            const name = String(el.getAttribute('name') || '').toLowerCase();
            const value = String(el.getAttribute('value') || '').toLowerCase();
            const href = String(el.getAttribute('href') || '').toLowerCase();
            const attrs = [
              el.id, name, value, href, el.getAttribute('aria-label'), el.getAttribute('title'),
              el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className,
            ].join(' ').toLowerCase();
            const text = norm(el.innerText || el.textContent || value);
            let score = 0;
            if (/\/(?:u\/)?log-?in\/password(?:[/?#]|$)/.test(href)) score += 100;
            if (name === 'intent' && /password/.test(value) && !/passwordless|reset|forgot/.test(value)) score += 90;
            if (/use[_-]?password|password[_-]?(?:login|signin)|(?:login|signin)[_-]?(?:with[_-]?)?password/.test(attrs)) score += 80;
            if (/使用密[码碼]|用密[码碼](?:登录|登入)|密[码碼](?:登录|登入)/.test(text)) score += 70;
            if (/use(?:your)?password|continuewithpassword|log(?:in)?withpassword|signinwithpassword/.test(text)) score += 70;
            if (/パスワード(?:を)?(?:使用|利用)|パスワードで(?:ログイン|続行)/.test(text)) score += 60;
            if (/비밀번호로(?:로그인|계속)/.test(text)) score += 60;
            if (/passwordless|one[-_\s]?time|otp|验证码|驗證碼|認証コード|reset|forgot|忘れ|찾기|新密码|新密碼/.test(`${attrs} ${text}`)) score -= 100;
            return {el, index, score, attrs: attrs.slice(0, 180), text: text.slice(0, 100),
              href: String(el.href || el.getAttribute('href') || '')};
          })
          .filter(item => item.score >= 60)
          .sort((a, b) => b.score - a.score || a.index - b.index);
        if (!candidates.length) return {ok:false, reason:'password_login_cta_missing'};
        const target = candidates[0];
        target.el.scrollIntoView({block:'center'});
        target.el.click();
        return {ok:true, score:target.score, attrs:target.attrs, text:target.text, href:target.href};
        """
    ) or {}
    if not result.get("ok"):
        logger.info("[Token刷新] 验证码页未找到密码登录入口，尝试密码页直达地址：%s", result)
        human_delay("navigate")
        driver.get("https://auth.openai.com/log-in/password")
    else:
        logger.info("[Token刷新] 已从邮箱验证码页切换到密码登录：%s", result)
        human_delay("navigate")

    target_href = str(result.get("href") or "").strip()
    parsed_href = urlparse(urljoin(str(getattr(driver, "current_url", "") or "https://auth.openai.com/"), target_href))
    parsed_path = parsed_href.path.lower()
    if (
        parsed_href.scheme.lower() not in {"http", "https"}
        or parsed_href.netloc.lower() != "auth.openai.com"
        or not any(path in parsed_path for path in ("/log-in/password", "/login/password"))
    ):
        target_href = ""
    elif target_href:
        target_href = parsed_href.geturl()

    end = time.time() + timeout
    while time.time() < end:
        if _is_login_password_page(driver):
            return "login_password"
        if _has_access_token(driver):
            return "logged_in"
        time.sleep(0.5)

    # Some SPA builds update the form only after a full navigation. Reuse the
    # exact href captured from the CTA so query/state parameters are retained.
    if target_href:
        current_url = str(getattr(driver, "current_url", "") or "").lower()
        if "email-verification" in current_url or "log-in/password" not in current_url:
            logger.info("[Token刷新] 密码入口点击后未识别到密码页，按 href 直达：%s", target_href)
            human_delay("navigate")
            driver.get(target_href)
            fallback_end = time.time() + min(15, timeout)
            while time.time() < fallback_end:
                if _is_login_password_page(driver):
                    return "login_password"
                if _has_access_token(driver):
                    return "logged_in"
                time.sleep(0.5)

    try:
        state = driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
              type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
              autocomplete: el.getAttribute('autocomplete') || '', id: el.id || ''
            })).slice(0, 20);
            const buttons = [...document.querySelectorAll('button,a,[role=button]')].filter(visible).map(el => ({
              tag: el.tagName, text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 100),
              href: el.getAttribute('href') || '', testid: el.getAttribute('data-testid') || ''
            })).slice(0, 20);
            return {url: location.href, title: document.title, inputs, buttons,
              text: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 800)};
            """
        ) or {}
    except Exception as exc:
        state = {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}
    raise RuntimeError(f"无法从邮箱验证码页切换到登录密码页: {state}")


def _complete_email_otp(driver, email: str, after_ts: float, max_attempts: int = 3) -> None:
    current_after_ts = after_ts
    current_code = None
    for attempt in range(1, max_attempts + 1):
        if current_code is None:
            current_code = wait_for_otp(email, after_ts=current_after_ts)
        _clear_otp_inputs(driver)
        _type_otp(driver, current_code)
        human_delay("otp_input")
        try:
            _click_continue(driver)
        except Exception:
            pass
        if _wait_after_email_otp_submit(driver, timeout=12) == "accepted":
            return
        if attempt >= max_attempts:
            break
        current_after_ts = time.time()
        _click_resend_email_otp(driver, timeout=25)
        current_code = None
    raise RuntimeError("邮箱验证码连续错误或过期，已达到最大重试次数")


def login_roxy_driver_with_password(
    driver,
    email: str,
    password: str,
    *,
    session_timeout: int = 100,
) -> dict:
    """在已有 Roxy/Cloak driver 中用密码登录并返回最新 ChatGPT session。"""
    for window_attempt in range(1, 3):
        try:
            driver.get("https://chatgpt.com/auth/login")
            human_delay("navigate")
            _maybe_accept(driver)

            _type_email_address(driver, email, timeout=25)
            human_delay("form")
            _submit_email_step(driver)
            password_submitted_at = None
            next_state = _wait_email_submit_next_state(driver, email, timeout=15)
            if next_state == "password":
                raise RuntimeError("邮箱进入注册密码页，拒绝执行注册流程")
            if next_state == "otp":
                next_state = _switch_email_verification_to_password(driver)
            if next_state == "login_password":
                for password_attempt in range(1, 3):
                    password_submitted_at = time.time()
                    _fill_login_password(driver, password)
                    next_state = _wait_after_password_submit(driver)
                    if next_state != "transient_error":
                        break
                    if password_attempt >= 2:
                        raise RuntimeError("密码登录连续两次遇到 auth.openai.com 临时错误页")
                    logger.info("[Token刷新] 准备从认证站点临时错误页恢复并重试密码登录")
                    _recover_from_transient_auth_error(driver)
            if next_state == "otp":
                if password_submitted_at is None:
                    raise RuntimeError("未提交密码却进入邮箱验证码分支，已停止以避免验证码优先登录")
                _complete_email_otp(driver, email, password_submitted_at)
            elif next_state not in {"logged_in", "login_password"}:
                raise RuntimeError(f"邮箱提交后进入了无法识别的状态: {next_state}")

            human_delay("post_auth")
            session_info = _fetch_chatgpt_session(driver, timeout=session_timeout, auto_jump_wait=10)
            if not str(session_info.get("accessToken") or "").strip():
                raise RuntimeError("/api/auth/session 未返回 accessToken")
            return session_info
        except Exception as exc:
            if window_attempt >= 2 or not _is_closed_window_error(exc) or not _activate_surviving_window(driver):
                raise
            logger.warning("[Token刷新] 认证标签页意外关闭，准备在存活标签页重新执行密码登录")
    raise RuntimeError("Token 更新密码登录未完成")


def run_roxy_token_refresh(email: str, password: str, proxy: str | None = None) -> dict:
    """返回 {ok, access_token, session_info}；失败返回 error，不泄露密码。"""
    email = str(email or "").strip()
    password = str(password or "")
    if not email:
        return {"ok": False, "status": "failed", "error": "邮箱为空"}
    if not password:
        return {"ok": False, "status": "failed", "error": "账号未保存密码"}

    client = RoxyBrowserClient()
    opened = None
    driver = None
    try:
        opened, driver = _open_token_refresh_browser(client)
        logger.info("[Token刷新] 开始登录：%s，profile=%s", email, opened.profile_id)
        session_info = login_roxy_driver_with_password(driver, email, password, session_timeout=100)
        access_token = str(session_info.get("accessToken") or "").strip()
        logger.info("[Token刷新] 登录成功：%s", email)
        return {
            "ok": True,
            "status": "success",
            "message": "Token 更新成功",
            "access_token": access_token,
            "session_info": {
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
            },
        }
    except Exception as exc:
        logger.warning("[Token刷新] 失败：%s，%s: %s", email, type(exc).__name__, str(exc)[:240])
        return {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            _cleanup_token_refresh_browser(client, opened, driver)
